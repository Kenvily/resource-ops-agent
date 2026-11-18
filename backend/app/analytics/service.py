import asyncio
import hashlib
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg import Error as DatabaseError

from app.clients.prometheus import SourceError

TZ = ZoneInfo("Asia/Shanghai")


class AnalyticsService:
    def __init__(self, history, settings):
        self.pool = history.store.pool if history else None
        self.enabled = settings.analytics_enabled
        self.last_cleanup = float("-inf")
        self.cleanup_lock = asyncio.Lock()

    async def record(self, batch):
        if not self.enabled:
            return {"accepted": 0, "enabled": False}
        if not self.pool:
            raise SourceError("使用统计需要PostgreSQL")
        visitor = hashlib.sha256(("roc-product-v1:" + str(batch.visitor_id)).encode()).hexdigest()
        accepted = 0
        async with self.pool.connection() as conn:
            await conn.execute("SET LOCAL statement_timeout=2000")
            for event in batch.events:
                row = await (
                    await conn.execute(
                        "INSERT INTO product_event(id,visitor_hash,page,kind,feature) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING occurred_at",
                        (event.id, visitor, event.page, event.kind, event.feature),
                    )
                ).fetchone()
                if row:
                    accepted += 1
                    await conn.execute(
                        "UPDATE product_analytics_metadata SET first_event_at=%s WHERE id=1 AND first_event_at IS NULL",
                        (row[0],),
                    )
        # Lazy, bounded maintenance. No scheduler or queue; failed cleanup cannot undo accepted events.
        if time.monotonic() - self.last_cleanup > 3600 and not self.cleanup_lock.locked():
            async with self.cleanup_lock:
                self.last_cleanup = time.monotonic()
                try:
                    async with self.pool.connection() as conn:
                        await conn.execute("SET LOCAL statement_timeout=2000")
                        await conn.execute(
                            "DELETE FROM product_event WHERE occurred_at < now()-interval '90 days'"
                        )
                except DatabaseError:
                    pass
        return {"accepted": accepted, "enabled": True}

    async def summary(self, days, now=None):
        if not self.enabled:
            return {"enabled": False}
        if not self.pool:
            raise SourceError("使用统计需要PostgreSQL")
        now = now or datetime.now(TZ)
        today = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        since = today - timedelta(days=days - 1)
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await conn.execute("SET LOCAL statement_timeout=5000")
            first = await (
                await conn.execute("SELECT first_event_at FROM product_analytics_metadata WHERE id=1")
            ).fetchone()
            totals = await (
                await conn.execute(
                    """SELECT count(*) FILTER(WHERE kind='page_view'),
                count(DISTINCT visitor_hash) FILTER(WHERE kind='page_view'),count(*) FILTER(WHERE kind='feature_use')
                FROM product_event WHERE occurred_at>=%s AND occurred_at<=%s""",
                    (since, now),
                )
            ).fetchone()
            daily = await (
                await conn.execute(
                    """SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date,
                count(*) FILTER(WHERE kind='page_view'),count(DISTINCT visitor_hash) FILTER(WHERE kind='page_view'),
                count(*) FILTER(WHERE kind='feature_use') FROM product_event WHERE occurred_at>=%s AND occurred_at<=%s GROUP BY 1 ORDER BY 1""",
                    (since, now),
                )
            ).fetchall()
            pages = await (
                await conn.execute(
                    """SELECT page,count(*) FILTER(WHERE kind='page_view'),count(*) FILTER(WHERE kind='feature_use')
                FROM product_event WHERE occurred_at>=%s AND occurred_at<=%s GROUP BY 1 ORDER BY 2 DESC,1""",
                    (since, now),
                )
            ).fetchall()
            features = await (
                await conn.execute(
                    """SELECT feature,count(*) FROM product_event WHERE kind='feature_use' AND occurred_at>=%s AND occurred_at<=%s GROUP BY 1 ORDER BY 2 DESC,1""",
                    (since, now),
                )
            ).fetchall()
            tasks = await (
                await conn.execute(
                    """SELECT kind,status,count(*),count(*) FILTER(WHERE finished_at IS NOT NULL),
                sum(extract(epoch FROM(finished_at-created_at))*1000) FILTER(WHERE finished_at IS NOT NULL),
                count(*) FILTER(WHERE jsonb_typeof(COALESCE(result->'model_calls',result->'agent'->'model_calls')) IS DISTINCT FROM 'number'),
                count(*) FILTER(WHERE CASE WHEN jsonb_typeof(COALESCE(result->'model_calls',result->'agent'->'model_calls'))='number'
                THEN (COALESCE(result->'model_calls',result->'agent'->'model_calls'))::numeric ELSE 0 END > 0)
                FROM investigation_task WHERE NOT legacy AND created_at>=%s AND created_at<=%s GROUP BY 1,2 ORDER BY 1,2""",
                    (since, now),
                )
            ).fetchall()
        by_day = {str(row[0]): row for row in daily}
        first_date = first[0].astimezone(TZ).date() if first and first[0] else None
        trend = []
        for offset in range(days):
            date = (since + timedelta(days=offset)).date()
            row = by_day.get(str(date))
            trend.append(
                {
                    "date": str(date),
                    "pv": (row[1] if row else 0) if first_date and date >= first_date else None,
                    "dau": (row[2] if row else 0) if first_date and date >= first_date else None,
                    "feature_uses": (row[3] if row else 0) if first_date and date >= first_date else None,
                }
            )
        states: dict[str, int] = {}
        kinds: dict[str, int] = {}
        measured, duration, model_tasks, unknown_model_tasks = 0, 0.0, 0, 0
        for kind, status, count, samples, elapsed, unknown_llm, llm_count in tasks:
            states[status] = states.get(status, 0) + count
            kinds[kind] = kinds.get(kind, 0) + count
            measured += samples
            duration += float(elapsed or 0)
            model_tasks += llm_count
            unknown_model_tasks += unknown_llm
        task_count = sum(states.values())
        terminal = task_count - states.get("running", 0)
        return {
            "enabled": True,
            "window_days": days,
            "timezone": "Asia/Shanghai",
            "start": since.timestamp(),
            "end": now.timestamp(),
            "tracking_started_at": first[0].timestamp() if first and first[0] else None,
            "pv": totals[0],
            "uv": totals[1],
            "dau": trend[-1]["dau"],
            "feature_uses": totals[2],
            "daily": trend,
            "pages": [{"page": p, "pv": pv, "feature_uses": uses} for p, pv, uses in pages],
            "features": [{"feature": f, "count": n} for f, n in features],
            "agents": {
                "tasks": task_count,
                "terminal_tasks": terminal,
                "model_tasks": model_tasks,
                "unknown_model_tasks": unknown_model_tasks,
                "success_rate": states.get("complete", 0) / terminal if terminal else None,
                "avg_duration_ms": duration / measured if measured else None,
                "duration_samples": measured,
                "statuses": states,
                "kinds": kinds,
            },
            "definitions": [
                "PV记录首次打开及页面切换；筛选、范围切换和自动刷新不增加PV。",
                "UV为所选时间内访问页面的匿名浏览器数；DAU为上海时区当天的匿名浏览器数，不等同实名人数。",
                "功能次数是已注册关键操作的触发次数，包含失败尝试；不表示全部点击或成功执行。",
                "任务按数据库任务ID去重，包含确定性查询、澄清与继续任务；已记录LLM任务单列，取消等无调用记录的任务保持未知。",
                "完整完成率=complete任务/非running任务；受限、澄清、拒绝、失败及取消不计成功，不代表结论业务正确率。",
                "平均耗时使用已结束任务的结束时间减创建时间，包含失败/取消；旧版导入记录排除。",
                "页面数据从埋点启用后积累，可能受拦截、离线或清除浏览器标识影响；不会补造历史访问。原始事件保留90天，收到事件时按小时惰性清理。",
            ],
        }
