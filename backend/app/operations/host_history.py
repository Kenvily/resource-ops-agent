"""Mergeable minute host facts and bounded DB-only trends; no query-time synchronization."""

import asyncio
import math
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clients.prometheus import SourceError
from app.core.time_ranges import four_months_ago, range_seconds

METRICS = {
    "cpu_utilization_pct": ("CPU 利用率", "%"),
    "cpu_cores": ("逻辑核数", "cores"),
    "memory_total_bytes": ("系统内存容量", "bytes"),
    "memory_used_bytes": ("系统内存已用", "bytes"),
    "memory_available_bytes": ("系统内存可用", "bytes"),
    "load1": ("1 分钟负载", "load"),
}


class HostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: Literal["yz", "zw"]
    node: str = Field(min_length=1, max_length=256)
    history_mode: Literal["statistics", "auto"] = "statistics"
    window: Literal["1h", "1d", "1w", "1mo", "3mo", "all"] = "1h"


class HostHistoryService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings
        self.pool = history.store.pool if history else None

    async def capture(self, conn, cluster, at, rows):
        if at % 3600 == 0:
            from app.operations.host_samples import capture

            await capture(conn, cluster, at, rows)
        for row in rows:
            for metric in METRICS:
                value = row.get(metric)
                await conn.execute(
                    """INSERT INTO host_metric_bucket VALUES(%s,%s,%s,60,%s,%s,%s,%s,%s)
                  ON CONFLICT(cluster,node,metric,grain,ts) DO UPDATE SET
                  sum_value=excluded.sum_value,min_value=excluded.min_value,max_value=excluded.max_value,valid_points=excluded.valid_points
                  WHERE excluded.valid_points>0 OR host_metric_bucket.valid_points=0""",
                    (cluster, row["node"], metric, at, value, value, value, int(value is not None)),
                )
        complete = bool(rows) and all(r.get(m) is not None for r in rows for m in METRICS)
        source_error = any(
            r.get("metric_statuses", {}).get(k) == "error"
            for r in rows
            for k in ("host_cpu", "host_cores", "host_memory_total", "host_memory_available")
        ) or any(r.get("extended_metric_statuses", {}).get("host_load1") == "error" for r in rows)
        status = "ready" if complete else "partial" if rows else "empty"
        code = (
            None
            if complete
            else "metric_unavailable"
            if source_error
            else "missing_metrics"
            if rows
            else "no_nodes"
        )
        await conn.execute(
            """INSERT INTO host_history_coverage(cluster,ts,expected_nodes,complete,attempt_status,failure_code) VALUES(%s,%s,%s,%s,%s,%s)
          ON CONFLICT(cluster,ts) DO UPDATE SET expected_nodes=excluded.expected_nodes,complete=excluded.complete,attempt_status=excluded.attempt_status,failure_code=excluded.failure_code,attempted_at=now()""",
            (cluster, at, len(rows), complete, status, code),
        )

    async def rollup(self, conn, cluster, start, end):
        for source, target in ((60, 300), (300, 3600)):
            await conn.execute(
                """INSERT INTO host_metric_bucket
              SELECT cluster,node,metric,%s,ts/%s*%s,sum(sum_value),min(min_value),max(max_value),sum(valid_points)::integer
              FROM host_metric_bucket WHERE cluster=%s AND grain=%s AND ts>=%s AND ts<%s
              GROUP BY 1,2,3,5
              ON CONFLICT(cluster,node,metric,grain,ts) DO UPDATE SET sum_value=excluded.sum_value,
              min_value=excluded.min_value,max_value=excluded.max_value,valid_points=excluded.valid_points""",
                (
                    target,
                    target,
                    target,
                    cluster,
                    source,
                    start // target * target,
                    end // target * target + target,
                ),
            )

    async def prune(self, now=None):
        now = int(time.time()) if now is None else now
        assert self.pool is not None
        async with self.pool.connection() as conn:
            # All rollups are written before finer data expires; clean only whole hours.
            for grain, cutoff in (
                (60, now - 2 * 86400),
                (300, now - 8 * 86400),
                (3600, four_months_ago(now)),
            ):
                await conn.execute(
                    "DELETE FROM host_metric_bucket WHERE grain=%s AND ts<%s", (grain, cutoff // 3600 * 3600)
                )
            for table in ("host_history_point", "host_point_coverage"):
                await conn.execute(f"DELETE FROM {table} WHERE ts<%s", (four_months_ago(now) // 3600 * 3600,))
            await conn.execute(
                "DELETE FROM host_history_coverage WHERE ts<%s", ((now - 2 * 86400) // 3600 * 3600,)
            )

    async def status(self, clusters, now=None):
        if not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准查询")
        if not self.pool:
            return {"available": False, "clusters": []}
        now = int(time.time()) if now is None else now
        left = (now - 2 * 86400) // 3600 * 3600
        right = (now - 120) // 60 * 60
        async with self.pool.connection() as conn:
            records = await (
                await conn.execute(
                    """SELECT cluster,count(*),count(*) FILTER(WHERE complete),
              count(*) FILTER(WHERE attempt_status='error'),count(*) FILTER(WHERE attempt_status='partial'),
              count(*) FILTER(WHERE attempt_status='empty'),count(*) FILTER(WHERE attempt_status='unknown'),
              extract(epoch from max(attempted_at)),max(ts) FILTER(WHERE complete)
              FROM host_history_coverage WHERE cluster=ANY(%s) AND ts BETWEEN %s AND %s GROUP BY cluster""",
                    (clusters, left, right),
                )
            ).fetchall()
        by_cluster = {r[0]: r for r in records}
        result = []
        expected = (right - left) // 60 + 1
        for cid in clusters:
            r = by_cluster.get(cid)
            attempted = int(r[1]) if r else 0
            complete = int(r[2]) if r else 0
            errors = int(r[3]) if r else 0
            state = (
                "disabled"
                if not self.settings.host_metrics_enabled
                else "not_started"
                if not attempted
                else "attention"
                if errors
                else "backfilling"
                if complete < expected
                else "ready"
            )
            result.append(
                {
                    "cluster": cid,
                    "state": state,
                    "window_start": left,
                    "window_end": right,
                    "expected_minutes": expected,
                    "attempted_minutes": attempted,
                    "complete_minutes": complete,
                    "pending_minutes": max(0, expected - complete),
                    "error_minutes": errors,
                    "partial_minutes": int(r[4]) if r else 0,
                    "empty_minutes": int(r[5]) if r else 0,
                    "unknown_minutes": int(r[6]) if r else 0,
                    "latest_attempt_at": float(r[7]) if r and r[7] else None,
                    "latest_complete_at": int(r[8]) if r and r[8] else None,
                }
            )
        return {"available": True, "database_read_at": time.time(), "clusters": result}

    async def detail(self, query, now=None):
        if query.cluster not in self.settings.insight_allowed_clusters:
            raise SourceError("所选集群未获准查询")
        if not self.pool:
            raise SourceError("主机资源需要 PostgreSQL 数据库")
        from app.operations.hosts import read_hosts

        current = next(
            (h for h in await read_hosts(self.history, [query.cluster]) if h["node"] == query.node), None
        )
        now = int(time.time()) if now is None else now
        grain = {"1h": 60, "1d": 300, "1w": 3600, "1mo": 3600, "3mo": 3600, "all": 3600}[query.window]
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            earliest = (
                await (
                    await conn.execute(
                        "SELECT min(ts) FROM host_metric_bucket WHERE cluster=%s AND node=%s AND grain=%s",
                        (query.cluster, query.node, grain),
                    )
                ).fetchone()
            )[0]
            sample_earliest = (
                await (
                    await conn.execute(
                        "SELECT min(ts) FROM host_history_point WHERE cluster=%s AND node=%s",
                        (query.cluster, query.node),
                    )
                ).fetchone()
            )[0]
            if grain == 3600 and query.history_mode == "auto" and sample_earliest is not None:
                earliest = min(earliest, sample_earliest) if earliest is not None else sample_earliest
            if current is None and earliest is None and sample_earliest is None:
                raise SourceError("机器不存在或不在已采集范围")
            duration = (
                max(grain, now - earliest)
                if query.window == "all" and earliest
                else range_seconds(query.window, now)
            )
            step = grain * max(1, math.ceil(duration / grain / 720))
            end = (now - 120) // step * step
            start = (end - duration) // step * step
            rows = await (
                await conn.execute(
                    """SELECT metric,ts/%s*%s,sum(sum_value),min(min_value),max(max_value),sum(valid_points)
              FROM host_metric_bucket WHERE cluster=%s AND node=%s AND grain=%s AND ts>=%s AND ts<%s
              GROUP BY 1,2 ORDER BY 1,2""",
                    (step, step, query.cluster, query.node, grain, start, end),
                )
            ).fetchall()
            sampled_rows = []
            if grain == 3600 and query.history_mode == "auto":
                sampled_rows = await (
                    await conn.execute(
                        """SELECT metric,ts,value FROM host_history_point
                    WHERE cluster=%s AND node=%s AND ts>=%s AND ts<%s AND ts%% %s=0""",
                        (query.cluster, query.node, start, end, step),
                    )
                ).fetchall()
        points = {(r[0], r[1]): r for r in rows}
        sampled = {(m, ts): value for m, ts, value in sampled_rows}
        statistic_counts = {m: sum(r[5] == step // 60 for r in rows if r[0] == m) for m in METRICS}
        sample_counts = {
            m: sum(v is not None for (metric, _), v in sampled.items() if metric == m) for m in METRICS
        }
        use_samples = all(sample_counts[m] >= statistic_counts[m] for m in METRICS) and any(
            sample_counts[m] > statistic_counts[m] for m in METRICS
        )
        series = []
        for metric, (label, unit) in METRICS.items():
            values = []
            for ts in range(start, end, step):
                row = points.get((metric, ts))
                valid = int(row[5]) if row else 0
                expected = 1 if use_samples else step // 60
                if use_samples:
                    valid = int(sampled.get((metric, ts)) is not None)
                values.append(
                    {
                        "timestamp": ts,
                        "sampled_value": sampled.get((metric, ts)) if use_samples else None,
                        "mean": row[2] / valid if not use_samples and row and valid == expected else None,
                        "min": row[3] if row and not use_samples else None,
                        "max": row[4] if row and not use_samples else None,
                        "valid_points": valid,
                        "expected_points": expected,
                        "coverage": valid / expected,
                    }
                )
            series.append({"metric": metric, "label": label, "unit": unit, "points": values})
        return {
            "cluster": query.cluster,
            "node": query.node,
            "current": current,
            "database_read_at": time.time(),
            "collection_enabled": self.settings.host_metrics_enabled,
            "history": {
                "start": start,
                "end": end - 60,
                "grain": step,
                "source_grain": grain,
                "sampling": "hourly_point" if use_samples else "minute_statistics",
                "series": series,
            },
            "definitions": {
                k: {"source": v}
                for k, v in {
                    "cpu_utilization_pct": "node_cpu_seconds_total: 近5分钟 idle rate，经逻辑核数核验",
                    "memory_used_bytes": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
                    "load1": "node_load1: 1分钟运行队列负载，不是CPU百分比",
                    "filesystem": "node_filesystem_size_bytes / avail_bytes: 按挂载点展示，不合计",
                    "network": "rate(node_network_*_bytes_total[5m]): 按接口展示，不合计",
                    "kubernetes": "kube_node_status_allocatable / spec_unschedulable / status_condition: 只读节点证据，不保证可调度",
                }.items()
            },
        }


async def fill_recent(history, cluster, now=None):
    from app.operations.hosts import BASE_TEMPLATES, host_aliases, normalize_hosts
    from app.operations.metrics import samples
    from app.resources.backfill import at_point

    now = int(time.time()) if now is None else now
    end = (now - 120) // 60 * 60
    start = (now - 2 * 86400) // 3600 * 3600
    async with history.store.pool.connection() as conn:
        known = await (
            await conn.execute(
                "SELECT ts FROM host_history_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s AND (complete OR attempted_at>now()-interval '10 minutes')",
                (cluster, start, end),
            )
        ).fetchall()
    missing = set(range(start, end + 1, 60)) - {r[0] for r in known}
    if not missing:
        return
    right = max(missing)
    left = right
    while left - 60 >= start and left - 60 in missing and right - left < 29 * 60:
        left -= 60
    client = history.resources.client
    try:
        cap, info = await asyncio.gather(
            *(client.range_query(cluster, key, left, right, 60) for key in ("eff_capacity", "ops_node_info")),
            return_exceptions=True,
        )
        inventory = set()
        node_info = []
        for ts in range(left, right + 1, 60):
            for labels, v in samples(at_point(cap, ts), cluster, ts):
                ids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                if v > 0 and len(ids) == 1 and labels.get("node"):
                    inventory.add((labels["node"], next(iter(ids))))
            node_info.extend(samples(at_point(info, ts), cluster, ts))
    except (SourceError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        async with history.store.pool.connection() as conn:
            for ts in range(left, right + 1, 60):
                await conn.execute(
                    """INSERT INTO host_history_coverage(cluster,ts,expected_nodes,complete,attempt_status,failure_code)
                  VALUES(%s,%s,0,false,'error','identity_unavailable') ON CONFLICT(cluster,ts) DO UPDATE SET
                  attempt_status='error',failure_code='identity_unavailable',attempted_at=now()""",
                    (cluster, ts),
                )
        return
    _, aliases = host_aliases(inventory, node_info)
    pattern = (
        "(" + "|".join(re.escape(a) for a in sorted(aliases)) + ")(:[0-9]+)?"
        if aliases
        else "__no_verified_gpu_host__"
    )
    if len(pattern) > 8000:
        return
    templates = [t for t in BASE_TEMPLATES if t != "host_dmi"] + ["host_load1"]
    payloads = (
        await asyncio.gather(
            *(client.range_query(cluster, t, left, right, 60, (pattern, "")) for t in templates),
            return_exceptions=True,
        )
        if inventory
        else [{} for _ in templates]
    )
    service = HostHistoryService(history, client.settings)
    async with history.store.pool.connection() as conn:
        for ts in range(left, right + 1, 60):
            point_inventory = set()
            for labels, v in samples(at_point(cap, ts), cluster, ts):
                ids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                if v > 0 and len(ids) == 1 and labels.get("node"):
                    point_inventory.add((labels["node"], next(iter(ids))))
            parts = {t: at_point(p, ts) for t, p in zip(templates, payloads)}
            rows = normalize_hosts(
                cluster, ts, point_inventory, list(samples(at_point(info, ts), cluster, ts)), parts
            )
            await service.capture(conn, cluster, ts, rows)
        await service.rollup(conn, cluster, left, right)
    await service.prune()
