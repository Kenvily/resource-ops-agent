"""Bounded minute backfill for the most recent two days; never triggered by page reads."""

import asyncio
import time

from app.operations.metrics import normalize, samples
from app.resources.identity import device_metadata
from app.resources.store import METRICS, ResourceStore


def at_point(payload, ts):
    if isinstance(payload, BaseException):
        return payload
    data = {**payload, "data": {**payload.get("data", {}), "result": []}}
    for row in payload.get("data", {}).get("result", []):
        values = [v for v in row.get("values", []) if v[0] == ts]
        if values:
            data["data"]["result"].append({**row, "values": values})
    return data


async def fill_recent(history, cluster):
    now = int(time.time())
    end = (now - 120) // 60 * 60
    start = (now - 2 * 86400) // 3600 * 3600
    async with history.store.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT ts FROM resource_history_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s AND (complete OR attempted_at>now()-interval '10 minutes')",
                (cluster, start, end),
            )
        ).fetchall()
    missing = set(range(start, end + 1, 60)) - {r[0] for r in rows}
    if not missing:
        return
    right = max(missing)
    left = max(start, right - 29 * 60)
    names = ["eff_capacity"] + ["ops_" + m for m in METRICS]
    payloads = await asyncio.gather(
        *(history.resources.client.range_query(cluster, name, left, right, 60) for name in names),
        return_exceptions=True,
    )
    store = ResourceStore(history.store)
    async with history.store.pool.connection() as conn:
        for ts in range(left, right + 1, 60):
            parts = [at_point(p, ts) for p in payloads]
            inventory = set()
            records = {}
            try:
                for labels, value in samples(parts[0], cluster, ts):
                    ids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                    if value > 0 and labels.get("node") and len(ids) == 1:
                        inventory.add((labels["node"], next(iter(ids))))
            except (ValueError, TypeError, KeyError, AttributeError):
                inventory = set()
            for metric, p in zip(METRICS, parts[1:]):
                try:
                    records[metric] = normalize(metric, p, cluster, ts, inventory, [])
                except (ValueError, TypeError, KeyError, AttributeError):
                    records[metric] = {"values": []}
            metadata = device_metadata(parts[2], cluster, ts, inventory)
            await store.capture(conn, cluster, ts, inventory, records, metadata)
