"""Measured database/process evidence; no automatic infrastructure provisioning."""

import time


def assessment(telemetry, database):
    rows = telemetry["metrics"]
    http = [r for r in rows if r["domain"] == "http" and r["operation"].startswith("/api/")]
    upstream = [r for r in rows if r["domain"] == "prometheus"]
    enough = sum(r["recent_samples"] for r in http) >= 30
    slow = [r for r in http if r["recent_samples"] >= 30 and r["p95_ms"] > 500]
    pressure = [r for r in upstream if r["count"] >= 30 and (r["error_rate"] > 0.05 or r["p95_ms"] > 5000)]
    large = database and database.get("database_bytes", 0) > 20 * 1024**3
    pool = database.get("pool", {}) if database else {}
    return [
        {
            "component": "共享缓存",
            "status": "investigate" if slow else "insufficient" if not enough else "keep",
            "reason": "已有慢路径，先定位热点、重复查询与进程间重复量"
            if slow
            else "未观测到充分的跨实例热点证据，继续使用现有缓存",
        },
        {
            "component": "数据库扩容/分区",
            "status": "investigate"
            if large or pool.get("requests_waiting", 0) > 0
            else "insufficient"
            if not database
            else "keep",
            "reason": "存储或连接等待需要进一步核验；结合增长率、保留策略与 EXPLAIN 决策"
            if large or pool.get("requests_waiting", 0) > 0
            else "当前容量与等待证据不足以支持分区或扩容",
        },
        {
            "component": "任务队列",
            "status": "investigate" if pressure else "insufficient" if not upstream else "keep",
            "reason": "上游压力偏高，先核验采集频率、限流与重试；尚无持久任务积压证据"
            if pressure
            else "当前采用有界进程内任务；未建立必须持久化/跨实例消费的任务需求",
        },
    ]


async def infrastructure(history, telemetry):
    db = None
    if history:
        async with history.store.pool.connection() as conn:
            size = await (await conn.execute("SELECT pg_database_size(current_database())")).fetchone()
            rows = await (
                await conn.execute(
                    "SELECT relname,n_live_tup,n_dead_tup,pg_total_relation_size(relid),extract(epoch from last_autovacuum) FROM pg_stat_user_tables WHERE schemaname=current_schema() ORDER BY pg_total_relation_size(relid) DESC LIMIT 20"
                )
            ).fetchall()
            activity = await (
                await conn.execute(
                    "SELECT count(*),count(*) FILTER(WHERE wait_event_type='Lock'),count(*) FILTER(WHERE state='active') FROM pg_stat_activity WHERE datname=current_database()"
                )
            ).fetchone()
        db = {
            "database_bytes": size[0],
            "tables": [
                {
                    "name": r[0],
                    "estimated_rows": r[1],
                    "estimated_dead_rows": r[2],
                    "total_bytes": r[3],
                    "last_autovacuum": float(r[4]) if r[4] else None,
                }
                for r in rows
            ],
            "connections": activity[0],
            "lock_waiters": activity[1],
            "active_queries": activity[2],
            "pool": history.store.pool.get_stats(),
        }
    stats = telemetry.snapshot()
    return {
        "observed_at": time.time(),
        "telemetry": stats,
        "database": db,
        "recommendations": assessment(stats, db),
        "limitations": [
            "进程内最近512次延迟；重启归零。HTTP为响应头耗时，流式模型完整耗时见对应调查结果。",
            "数据库行数为统计估计；当前观测不代表生产并发或长期增长趋势。",
            "阈值仅触发进一步排查，不自动安装缓存、队列或修改数据库。",
        ],
    }
