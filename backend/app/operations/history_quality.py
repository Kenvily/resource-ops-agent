"""Bounded host-history quality summaries shared by controlled investigation tools."""

import time

from app.clients.prometheus import SourceError
from app.operations.host_history import METRICS


class HistoryQualityService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings

    def authorize(self, cluster):
        if cluster not in self.settings.insight_allowed_clusters:
            raise SourceError("集群未获准查询")

    async def coverage(self, cluster, metric, start, end, grain, node=None):
        self.authorize(cluster)
        if metric not in METRICS or grain not in (60, 300, 3600) or end < start:
            raise SourceError("历史质量查询参数无效")
        # Summaries do not export host names, IPs or unbounded series. An observed-node
        # cohort is not proof that every expected historical host was scraped.
        async with self.history.store.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            statistics = await (
                await conn.execute(
                    """SELECT count(*),count(*) FILTER(WHERE valid= %s),max(ts)
                FROM (SELECT ts,min(valid_points) AS valid
                FROM host_metric_bucket WHERE cluster=%s AND metric=%s AND grain=%s
                AND ts BETWEEN %s AND %s AND (%s::text IS NULL OR node=%s) GROUP BY ts) q""",
                    (grain // 60, cluster, metric, grain, start, end, node, node),
                )
            ).fetchone()
            points = await (
                await conn.execute(
                    """SELECT count(DISTINCT ts),max(ts) FROM host_history_point
                WHERE cluster=%s AND metric=%s AND value IS NOT NULL AND ts BETWEEN %s AND %s AND (%s::text IS NULL OR node=%s)""",
                    (cluster, metric, start, end, node, node),
                )
            ).fetchone()
        expected = max(0, end // grain - (start + grain - 1) // grain + 1)
        return {
            "metric": metric,
            "label": METRICS[metric][0],
            "unit": METRICS[metric][1],
            "start": start,
            "end": end,
            "grain": grain,
            "expected_grid_points": expected,
            "statistics_observed_timestamps": statistics[0],
            "statistics_complete_observed_cohort_timestamps": statistics[1],
            "statistics_latest_at": statistics[2],
            "hourly_instant_observed_timestamps": points[0],
            "hourly_instant_latest_at": points[1],
            "database_read_at": time.time(),
            "source": "postgresql",
            "limitations": [
                "覆盖仅针对数据库已观测节点，不代表完整历史资产清单",
                "小时采样不是整小时均值、峰值或持续性证据",
                "数据库无记录不能证明Prometheus无数据；本工具不访问源端",
            ],
        }

    async def sync_status(self, cluster, start, end):
        self.authorize(cluster)
        async with self.history.store.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            recent = await (
                await conn.execute(
                    """SELECT count(*),count(*) FILTER(WHERE complete),
                count(*) FILTER(WHERE attempt_status='error'),extract(epoch FROM max(attempted_at))
                FROM host_history_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s""",
                    (cluster, start, end),
                )
            ).fetchone()
            hourly = await (
                await conn.execute(
                    """SELECT count(*),count(*) FILTER(WHERE complete),extract(epoch FROM max(attempted_at))
                FROM host_point_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s""",
                    (cluster, start, end),
                )
            ).fetchone()
        return {
            "start": start,
            "end": end,
            "source": "postgresql",
            "minute_attempted": recent[0],
            "minute_complete": recent[1],
            "minute_error": recent[2],
            "minute_latest_attempt_at": float(recent[3]) if recent[3] else None,
            "hourly_point_attempted": hourly[0],
            "hourly_point_complete": hourly[1],
            "hourly_latest_attempt_at": float(hourly[2]) if hourly[2] else None,
            "hourly_expected": max(0, end // 3600 - (start + 3599) // 3600 + 1),
            "source_retention_verified": False,
            "limitation": "检查点只证明数据库记录的尝试；没有检查点不等于源端缺失，旧分钟检查点可能已按策略清理",
        }
