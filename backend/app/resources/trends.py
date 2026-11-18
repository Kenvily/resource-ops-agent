"""Bounded multi-resource cluster trends from persisted buckets only."""

import math
import time
from collections import defaultdict
from typing import Literal

from pydantic import ConfigDict

from app.core.config import CLUSTERS
from app.core.time_ranges import range_seconds
from app.resources.models import StatisticsRequest


class FleetTrendRequest(StatisticsRequest):
    model_config = ConfigDict(extra="forbid")
    history_mode: Literal["statistics", "auto"] = "statistics"
    window: Literal["1h", "1d", "1w", "1mo", "3mo", "all"] = "1d"
    metric: Literal["gpu_util", "gpu_memory", "cpu_utilization_pct", "memory_used_bytes", "load1"]


async def fleet_trends(service, query):
    clusters = service.clusters(query.cluster)
    gpu = query.metric in ("gpu_util", "gpu_memory")
    table = "resource_metric_bucket" if gpu else "host_metric_bucket"
    grain = {"1h": 60, "1d": 300, "1w": 3600, "1mo": 3600, "3mo": 3600, "all": 3600}[query.window]
    metrics = [query.metric] + (["cpu_cores"] if query.metric == "cpu_utilization_pct" else [])
    now = int(time.time())
    async with service.store.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        earliest = (
            await (
                await conn.execute(
                    f"SELECT min(ts) FROM {table} WHERE cluster=ANY(%s) AND grain=%s AND metric=%s",
                    (clusters, grain, query.metric),
                )
            ).fetchone()
        )[0]
        if not gpu and grain == 3600 and query.history_mode == "auto":
            sample_earliest = (
                await (
                    await conn.execute(
                        "SELECT min(ts) FROM host_history_point WHERE cluster=ANY(%s) AND metric=%s",
                        (clusters, query.metric),
                    )
                ).fetchone()
            )[0]
            if sample_earliest is not None:
                earliest = min(earliest, sample_earliest) if earliest is not None else sample_earliest
        duration = (
            max(grain, now - earliest)
            if query.window == "all" and earliest
            else range_seconds(query.window if query.window != "all" else "3mo", now)
        )
        step = grain * max(1, math.ceil(duration / grain / 720))
        end = (now - 120) // step * step
        start = (end - duration) // step * step
        identity = "node,device_uuid" if gpu else "node"
        group_by = "1,2,3,4,5" if gpu else "1,2,3,4"
        time_column = 5 if gpu else 4
        rows = await (
            await conn.execute(
                f"""SELECT cluster,{identity},metric,ts/%s*%s,
        sum(sum_value),min(min_value),max(max_value),sum(valid_points)
        FROM {table} WHERE cluster=ANY(%s) AND grain=%s AND ts>=%s AND ts<%s AND metric=ANY(%s)
        GROUP BY {group_by} ORDER BY 1,{time_column}""",
                (step, step, clusters, grain, start, end, metrics),
            )
        ).fetchall()
        sampled_rows = []
        if not gpu and grain == 3600 and query.history_mode == "auto":
            sampled_rows = await (
                await conn.execute(
                    """SELECT cluster,node,metric,ts,value FROM host_history_point
                WHERE cluster=ANY(%s) AND metric=ANY(%s) AND ts>=%s AND ts<%s AND ts%% %s=0""",
                    (clusters, metrics, start, end, step),
                )
            ).fetchall()
    groups: dict = defaultdict(dict)
    for r in rows:
        cluster = r[0]
        metric, ts, summed, minimum, maximum, valid = r[-6:]
        entity = r[1:-6]
        groups[(cluster, ts)].setdefault(entity, {})[metric] = (summed, minimum, maximum, int(valid))
    sampled_groups: dict = defaultdict(dict)
    for cid, node, metric, ts, value in sampled_rows:
        sampled_groups[(cid, ts)].setdefault((node,), {})[metric] = (
            value,
            value,
            value,
            int(value is not None),
        )

    def available(source, expected, cid):
        return sum(
            bool(entities)
            and all(all(m in e and e[m][3] == expected for m in metrics) for e in entities.values())
            for (cluster, _), entities in source.items()
            if cluster == cid
        )

    use_samples = all(
        available(sampled_groups, 1, c) >= available(groups, step // 60, c) for c in clusters
    ) and any(available(sampled_groups, 1, c) > available(groups, step // 60, c) for c in clusters)
    if use_samples:
        groups = sampled_groups
    expected = 1 if use_samples else step // 60
    series = []
    for cid in clusters:
        points = []
        for ts in range(start, end, step):
            entities = groups.get((cid, ts), {})
            value = None
            if entities and all(
                query.metric in e and e[query.metric][3] == expected for e in entities.values()
            ):
                means = [e[query.metric][0] / e[query.metric][3] for e in entities.values()]
                if query.metric == "gpu_util":
                    value = sum(means) / len(means)
                elif query.metric == "cpu_utilization_pct":
                    cores = [e.get("cpu_cores") for e in entities.values()]
                    if all(c and c[3] == expected and c[1] == c[2] and c[1] > 0 for c in cores):
                        value = sum(v * c[1] for v, c in zip(means, cores)) / sum(c[1] for c in cores)
                else:
                    value = sum(means)
            points.append(
                {
                    "timestamp": ts,
                    "value": value,
                    "capacity_raw": None,
                    "allocated_raw": None,
                    "unallocated_raw": None,
                    "allocation_ratio": None,
                }
            )
        observed = sum(p["value"] is not None for p in points)
        series.append(
            {
                "id": cid,
                "label": CLUSTERS[cid][0],
                "status": "healthy" if observed == len(points) else "degraded" if observed else "empty",
                "points": points,
                "warnings": [] if observed == len(points) else ["历史有缺口，未补零。"],
                "database_read_at": time.time(),
                "expected_points": len(points),
                "observed_points": observed,
                "missing_points": len(points) - observed,
            }
        )
    return {
        "mode": service.settings.data_mode,
        "units": service.history.resources.units().model_dump(),
        "window": query.window,
        "metric": query.metric,
        "sampling": "hourly_point" if use_samples else "minute_statistics",
        "start": start,
        "end": end - step,
        "step": step,
        "fetched_at": time.time(),
        "cache_status": "miss",
        "cache_age_seconds": 0,
        "complete": all(s["status"] == "healthy" for s in series),
        "series": series,
    }
