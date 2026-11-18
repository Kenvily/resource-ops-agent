"""Hourly instant observations, never represented as hourly means or extrema."""

import asyncio
import re
import time

from app.core.time_ranges import four_months_ago
from app.operations.host_coarse import indexed_points
from app.operations.host_history import METRICS
from app.operations.hosts import BASE_TEMPLATES, host_aliases, normalize_hosts
from app.operations.metrics import samples
from app.resources.backfill import at_point


async def capture(conn, cluster, at, rows):
    complete = bool(rows) and all(r.get(m) is not None for r in rows for m in METRICS)
    for row in rows:
        for metric in METRICS:
            await conn.execute(
                """INSERT INTO host_history_point VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(cluster,node,metric,ts) DO UPDATE SET value=excluded.value
                WHERE excluded.value IS NOT NULL""",
                (cluster, row["node"], metric, at, row.get(metric)),
            )
    await conn.execute(
        """INSERT INTO host_point_coverage(cluster,ts,complete) VALUES(%s,%s,%s)
        ON CONFLICT(cluster,ts) DO UPDATE SET complete=host_point_coverage.complete OR excluded.complete,
        attempted_at=now()""",
        (cluster, at, complete),
    )
    return complete


def next_budget(current, cap, errors, successes):
    """Shrink on transport errors; recover after three consecutive successful batches."""
    if errors:
        return max(1, current // 2), 0
    successes += 1
    return (min(cap, current + max(1, current // 4)), 0) if successes >= 3 else (current, successes)


async def fill_points(history, cluster, *, start=None, end=None, budget=24):
    began = time.monotonic()
    now = int(time.time())
    left_bound = four_months_ago(now) if start is None else start
    start = (left_bound + 3599) // 3600 * 3600
    end = (now - 120) // 3600 * 3600 - 3600 if end is None else end // 3600 * 3600
    retry = getattr(history, "host_coarse_retry_seconds", 600)
    async with history.store.pool.connection() as conn:
        known = await (
            await conn.execute(
                """SELECT ts FROM host_point_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s
            AND (complete OR attempted_at>now()-(%s*interval '1 second'))""",
                (cluster, start, end, retry),
            )
        ).fetchall()
    missing = sorted(set(range(start, end + 1, 3600)) - {r[0] for r in known})
    history.host_coarse_report = {"strategy": "hourly_point", "complete_points": 0, "source_errors": 0}
    if not missing:
        return 0
    cap = max(1, min(budget, history.resources.settings.range_max_points, 168))
    states = getattr(history, "host_sample_budgets", {})
    current, successes = states.get(cluster, (cap, 0))
    current = min(current, cap)
    oldest = getattr(history, "host_coarse_oldest_first", False)
    left = missing[0] if oldest else max(start, missing[-1] - (current - 1) * 3600)
    right = min(end, left + (current - 1) * 3600) if oldest else missing[-1]
    client = history.resources.client
    cap_data, info = await asyncio.gather(
        *(client.range_query(cluster, key, left, right, 3600) for key in ("eff_capacity", "ops_node_info")),
        return_exceptions=True,
    )
    inventory, infos, identities = set(), [], {}
    for at in range(left, right + 1, 3600):
        ids, nodes = set(), []
        try:
            for labels, value in samples(at_point(cap_data, at), cluster, at):
                uuids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                if value > 0 and labels.get("node") and len(uuids) == 1:
                    ids.add((labels["node"], next(iter(uuids))))
            nodes = list(samples(at_point(info, at), cluster, at))
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        inventory.update(ids)
        infos.extend(nodes)
        identities[at] = (ids, nodes)
    _, aliases = host_aliases(inventory, infos)
    pattern = "(" + "|".join(re.escape(a) for a in sorted(aliases)) + ")(:[0-9]+)?"
    if len(pattern) > 8000:
        raise ValueError("host identity budget exceeded")
    templates = [t for t in BASE_TEMPLATES if t != "host_dmi"] + ["host_load1"]
    payloads = (
        await asyncio.gather(
            *(client.range_query(cluster, t, left, right, 3600, (pattern, "")) for t in templates),
            return_exceptions=True,
        )
        if inventory
        else [{} for _ in templates]
    )
    errors = sum(isinstance(p, BaseException) for p in (cap_data, info, *payloads))
    states[cluster] = next_budget(current, cap, errors, successes)
    history.host_sample_budgets = states
    # No cooldown for a failed large batch: immediately retry its smaller prefix next tick.
    if errors and current > 1:
        history.host_coarse_report.update(
            source_errors=errors,
            next_batch_hours=states[cluster][0],
            elapsed_seconds=round(time.monotonic() - began, 3),
        )
        return (right - left) // 3600 + 1
    indexed = [indexed_points(p) for p in payloads]
    complete = 0
    async with history.store.pool.connection() as conn:
        for at in range(left, right + 1, 3600):
            ids, nodes = identities[at]
            rows = normalize_hosts(
                cluster,
                at,
                ids,
                nodes,
                {t: index.get(at, at_point(p, at)) for t, p, index in zip(templates, payloads, indexed)},
            )
            complete += int(await capture(conn, cluster, at, rows))
        cutoff = four_months_ago(now) // 3600 * 3600
        await conn.execute("DELETE FROM host_history_point WHERE cluster=%s AND ts<%s", (cluster, cutoff))
        await conn.execute("DELETE FROM host_point_coverage WHERE cluster=%s AND ts<%s", (cluster, cutoff))
    history.host_coarse_report.update(
        complete_points=complete,
        source_errors=errors,
        next_batch_hours=states[cluster][0],
        elapsed_seconds=round(time.monotonic() - began, 3),
    )
    return (right - left) // 3600 + 1
