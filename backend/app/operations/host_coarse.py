"""Batch minute queries, persist only mergeable hourly host facts."""

import asyncio
import re
import time
from collections import defaultdict

from app.core.time_ranges import four_months_ago
from app.operations.host_history import METRICS
from app.operations.hosts import BASE_TEMPLATES, host_aliases, normalize_hosts
from app.operations.metrics import samples
from app.resources.backfill import at_point


def indexed_points(payload):
    if isinstance(payload, BaseException) or not isinstance(payload, dict):
        return {}
    result: dict = {}
    for row in payload.get("data", {}).get("result", []):
        for ts, value in row.get("values", []):
            point = result.setdefault(ts, {**payload, "data": {"resultType": "matrix", "result": []}})
            point["data"]["result"].append({"metric": row.get("metric", {}), "values": [[ts, value]]})
    return result


async def fill_hours(history, cluster, *, start=None, end=None, budget=6):
    history.host_coarse_report = {"full_hours": 0, "source_errors": 0}
    now = int(time.time())
    end = (now - 120) // 3600 * 3600 - 3600 if end is None else end // 3600 * 3600
    start = (four_months_ago(now) + 3599) // 3600 * 3600 if start is None else (start + 3599) // 3600 * 3600
    async with history.store.pool.connection() as conn:
        records = await (
            await conn.execute(
                """SELECT ts FROM host_metric_bucket WHERE cluster=%s AND grain=3600 AND ts BETWEEN %s AND %s
        GROUP BY ts HAVING min(valid_points)=60 AND count(DISTINCT metric)=6 AND count(*)=count(DISTINCT node)*6""",
                (cluster, start, end),
            )
        ).fetchall()
    attempted = getattr(history, "host_coarse_attempts", {})
    history.host_coarse_attempts = {
        k: v
        for k, v in attempted.items()
        if v > time.monotonic() - getattr(history, "host_coarse_retry_seconds", 600)
    }
    missing = sorted(
        set(range(start, end + 1, 3600))
        - {r[0] for r in records}
        - {t for (c, t) in history.host_coarse_attempts if c == cluster}
    )
    if not missing:
        return 0
    budget = max(
        1,
        min(
            budget,
            getattr(history, "host_coarse_budget", 12),
            history.resources.settings.range_max_points // 60,
            12,
        ),
    )
    if getattr(history, "host_coarse_oldest_first", False):
        left = missing[0]
        right = min(end, left + (budget - 1) * 3600)
    else:
        right = missing[-1]
        left = max(start, right - (budget - 1) * 3600)
    client = history.resources.client
    cap, info = await asyncio.gather(
        *(
            client.range_query(cluster, k, left + 3540, right + 3540, 3600)
            for k in ("eff_capacity", "ops_node_info")
        ),
        return_exceptions=True,
    )
    inventory = set()
    infos = []
    points = {}
    for hour in range(left, right + 1, 3600):
        ids = set()
        nodes = []
        try:
            for labels, value in samples(at_point(cap, hour + 3540), cluster, hour + 3540):
                uuids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                if value > 0 and labels.get("node") and len(uuids) == 1:
                    ids.add((labels["node"], next(iter(uuids))))
            nodes = list(samples(at_point(info, hour + 3540), cluster, hour + 3540))
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        points[hour] = (ids, nodes)
        inventory.update(ids)
        infos.extend(nodes)
    _, aliases = host_aliases(inventory, infos)
    pattern = "(" + "|".join(re.escape(a) for a in sorted(aliases)) + ")(:[0-9]+)?"
    if len(pattern) > 8000:
        raise ValueError("host identity budget exceeded")
    templates = [t for t in BASE_TEMPLATES if t != "host_dmi"] + ["host_load1"]
    payloads = (
        await asyncio.gather(
            *(client.range_query(cluster, t, left, right + 3540, 60, (pattern, "")) for t in templates),
            return_exceptions=True,
        )
        if inventory
        else [{} for _ in templates]
    )
    errors = sum(isinstance(p, BaseException) for p in [cap, info, *payloads])
    if errors and budget > 1:
        history.host_coarse_budget = max(1, budget // 2)
        return await fill_hours(history, cluster, start=start, end=end, budget=history.host_coarse_budget)
    indexed = [indexed_points(p) for p in payloads]
    full_hours = 0
    async with history.store.pool.connection() as conn:
        for hour in range(left, right + 1, 3600):
            ids, nodes = points[hour]
            facts = defaultdict(list)
            for ts in range(hour, hour + 3600, 60):
                rows = normalize_hosts(
                    cluster,
                    ts,
                    ids,
                    nodes,
                    {
                        t: idx[ts] if ts in idx else at_point(p, ts)
                        for t, p, idx in zip(templates, payloads, indexed)
                    },
                )
                for row in rows:
                    for metric in METRICS:
                        value = row.get(metric)
                        if value is not None:
                            facts[(row["node"], metric)].append(value)
            complete = bool(ids)
            for node in {n for n, _ in ids}:
                for metric in METRICS:
                    values = facts[(node, metric)]
                    valid = len(values)
                    complete = complete and valid == 60
                    await conn.execute(
                        """INSERT INTO host_metric_bucket VALUES(%s,%s,%s,3600,%s,%s,%s,%s,%s)
                    ON CONFLICT(cluster,node,metric,grain,ts) DO UPDATE SET sum_value=excluded.sum_value,min_value=excluded.min_value,
                    max_value=excluded.max_value,valid_points=excluded.valid_points WHERE excluded.valid_points>=host_metric_bucket.valid_points""",
                        (
                            cluster,
                            node,
                            metric,
                            hour,
                            sum(values) if values else None,
                            min(values) if values else None,
                            max(values) if values else None,
                            valid,
                        ),
                    )
            full_hours += int(complete)
            history.host_coarse_attempts[(cluster, hour)] = time.monotonic()
    history.host_coarse_report = {"full_hours": full_hours, "source_errors": errors}
    return (right - left) // 3600 + 1
