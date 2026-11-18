"""Bounded coarse backfill; retain better archived evidence on degraded rechecks."""

import asyncio
import time

from app.core.time_ranges import four_months_ago
from app.operations.catalog import CATALOG
from app.operations.metrics import normalize, samples
from app.resources.backfill import at_point
from app.resources.identity import device_metadata
from app.resources.store import METRICS


def combine_hour(parts):
    if not all(p is not None for p in parts):
        return (None, None, None, 0)
    summed, minimum, maximum, count = parts
    if count != int(count) or not 1 <= count <= 60 or minimum > maximum:
        return (None, None, None, 0)
    mean = summed / count
    tolerance = max(abs(mean), 1) * 1e-8
    if not minimum - tolerance <= mean <= maximum + tolerance:
        return (None, None, None, 0)
    return summed, minimum, maximum, int(count)


async def fill_hours(history, cluster):
    now = int(time.time())
    end = (now - 2 * 86400) // 3600 * 3600 - 3600
    first = (four_months_ago(now) + 3599) // 3600 * 3600
    async with history.store.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT ts FROM resource_coarse_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s AND (complete OR attempted_at>now()-interval '10 minutes')",
                (cluster, first, end),
            )
        ).fetchall()
    missing = set(range(first, end + 1, 3600)) - {r[0] for r in rows}
    if not missing:
        return
    right = max(missing)
    left = max(first, right - (history.resources.settings.resource_backfill_hours_per_tick - 1) * 3600)
    names = ["res_capacity_presence"] + [
        f"res_{m}_{stat}" for m in METRICS for stat in ("sum", "min", "max", "count")
    ]
    payloads = await asyncio.gather(
        *(
            history.resources.client.range_query(cluster, name, left + 3540, right + 3540, 3600)
            for name in names
        ),
        return_exceptions=True,
    )
    async with history.store.pool.connection() as conn:
        for ts in range(left, right + 1, 3600):
            at = ts + 3540
            parts = [at_point(p, at) for p in payloads]
            inventory = set()
            try:
                for labels, value in samples(parts[0], cluster, at):
                    ids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                    if value > 0 and labels.get("node") and len(ids) == 1:
                        inventory.add((labels["node"], next(iter(ids))))
            except (ValueError, TypeError, KeyError, AttributeError):
                inventory = set()
            metadata = device_metadata(parts[1], cluster, at, inventory)
            facts = {}
            for i, m in enumerate(METRICS):
                for j, stat in enumerate(("sum", "min", "max", "count")):
                    definition = CATALOG[m]
                    upper = 60 if stat == "count" else definition[4] * 60 if stat == "sum" else definition[4]
                    try:
                        normalized = normalize(
                            m,
                            parts[1 + i * 4 + j],
                            cluster,
                            at,
                            inventory,
                            [],
                            limits=(*definition[:3], 0, upper),
                        )
                        facts[(m, stat)] = {
                            (v["node"], v["device_uuid"]): v["value"] for v in normalized["values"]
                        }
                    except (ValueError, TypeError, KeyError, AttributeError):
                        facts[(m, stat)] = {}
            complete = bool(inventory)
            for node, uuid in sorted(inventory):
                model, driver = metadata.get((node, uuid), (None, None))
                await conn.execute(
                    """INSERT INTO resource_device VALUES(%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT(cluster,node,device_uuid) DO UPDATE SET first_seen=least(resource_device.first_seen,excluded.first_seen),last_seen=greatest(resource_device.last_seen,excluded.last_seen)""",
                    (cluster, node, uuid, model, driver, ts, at),
                )
                for metric in METRICS:
                    values = combine_hour(
                        [facts[(metric, stat)].get((node, uuid)) for stat in ("sum", "min", "max", "count")]
                    )
                    complete = complete and values[3] == 60
                    await conn.execute(
                        """INSERT INTO resource_metric_bucket VALUES(%s,%s,%s,%s,3600,%s,%s,%s,%s,%s)
                    ON CONFLICT(cluster,node,device_uuid,metric,grain,ts) DO UPDATE SET
                    sum_value=excluded.sum_value,min_value=excluded.min_value,max_value=excluded.max_value,valid_points=excluded.valid_points
                    WHERE excluded.valid_points>resource_metric_bucket.valid_points""",
                        (cluster, node, uuid, metric, ts, *values),
                    )
            await conn.execute(
                "INSERT INTO resource_coarse_coverage(cluster,ts,complete) VALUES(%s,%s,%s) ON CONFLICT(cluster,ts) DO UPDATE SET complete=excluded.complete,attempted_at=now()",
                (cluster, ts, complete),
            )
