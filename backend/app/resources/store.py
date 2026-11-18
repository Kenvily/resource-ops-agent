"""Idempotent raw facts and mergeable rollups; no averages of averages."""

import time

from app.core.time_ranges import four_months_ago

METRICS = ("gpu_util", "gpu_memory", "gpu_memory_free")


class ResourceStore:
    def __init__(self, history_store):
        self.pool = history_store.pool

    async def apply_bindings(self, bindings):
        if bindings is None:
            return
        now = int(time.time())
        desired = {(b.cluster, b.namespace_name, b.pod_name, b.container_name): b for b in bindings}
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(751000)")
            current = await (
                await conn.execute(
                    "SELECT cluster,namespace_name,pod_name,container_name,model_key,effective_from FROM resource_model_binding WHERE effective_until IS NULL FOR UPDATE"
                )
            ).fetchall()
            active = {r[:4]: r[4] for r in current}
            for row in current:
                b = desired.get(row[:4])
                if b is None or b.model_key != row[4]:
                    await conn.execute(
                        "UPDATE resource_model_binding SET effective_until=%s WHERE cluster=%s AND namespace_name=%s AND pod_name=%s AND container_name=%s AND effective_from=%s",
                        (max(now, row[5]), *row[:4], row[5]),
                    )
            for key, b in desired.items():
                await conn.execute(
                    "INSERT INTO resource_business_model VALUES(%s,%s) ON CONFLICT(model_key) DO UPDATE SET display_name=excluded.display_name",
                    (b.model_key, b.display_name),
                )
                if active.get(key) != b.model_key:
                    # Same-second changes remain additive rather than overwriting old versions.
                    previous = await (
                        await conn.execute(
                            "SELECT max(effective_from) FROM resource_model_binding WHERE cluster=%s AND namespace_name=%s AND pod_name=%s AND container_name=%s",
                            key,
                        )
                    ).fetchone()
                    start = max(now, (previous[0] or 0) + 1)
                    await conn.execute(
                        "INSERT INTO resource_model_binding VALUES(%s,%s,%s,%s,%s,%s,NULL)",
                        (*key, b.model_key, start),
                    )

    async def capture(self, conn, cluster, at, inventory, records, metadata):
        values = {
            key: {(v["node"], v["device_uuid"]): v["value"] for v in rec["values"]}
            for key, rec in records.items()
        }
        for node, uuid in sorted(inventory):
            model, driver = metadata.get((node, uuid), (None, None))
            await conn.execute(
                """INSERT INTO resource_device VALUES(%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT(cluster,node,device_uuid) DO UPDATE SET
             gpu_model=CASE WHEN excluded.last_seen>=resource_device.last_seen THEN excluded.gpu_model ELSE resource_device.gpu_model END,
             driver_version=CASE WHEN excluded.last_seen>=resource_device.last_seen THEN excluded.driver_version ELSE resource_device.driver_version END,
             first_seen=least(resource_device.first_seen,excluded.first_seen),last_seen=greatest(resource_device.last_seen,excluded.last_seen)""",
                (cluster, node, uuid, model, driver, at, at),
            )
            for metric in METRICS:
                v = values.get(metric, {}).get((node, uuid))
                await conn.execute(
                    """INSERT INTO resource_metric_bucket VALUES(%s,%s,%s,%s,60,%s,%s,%s,%s,%s)
                ON CONFLICT(cluster,node,device_uuid,metric,grain,ts) DO UPDATE SET sum_value=excluded.sum_value,min_value=excluded.min_value,max_value=excluded.max_value,valid_points=excluded.valid_points
                    WHERE excluded.valid_points>0 OR resource_metric_bucket.valid_points=0""",
                    (cluster, node, uuid, metric, at, v, v, v, int(v is not None)),
                )
        complete = bool(inventory) and all(len(values.get(m, {})) == len(inventory) for m in METRICS)
        await conn.execute(
            "INSERT INTO resource_history_coverage(cluster,ts,expected_devices,complete) VALUES(%s,%s,%s,%s) ON CONFLICT(cluster,ts) DO UPDATE SET expected_devices=excluded.expected_devices,complete=excluded.complete,attempted_at=now()",
            (cluster, at, len(inventory), complete),
        )
        await self.rollup(conn, cluster, at, at)

    async def rollup(self, conn, cluster, start, end):
        for source, target in ((60, 300), (300, 3600)):
            left = start // target * target
            right = end // target * target + target
            await conn.execute(
                """INSERT INTO resource_metric_bucket
            SELECT cluster,node,device_uuid,metric,%s,ts/ %s * %s,
                   sum(sum_value),min(min_value),max(max_value),sum(valid_points)::integer
            FROM resource_metric_bucket WHERE cluster=%s AND grain=%s AND ts>=%s AND ts<%s
            GROUP BY 1,2,3,4,6
            ON CONFLICT(cluster,node,device_uuid,metric,grain,ts) DO UPDATE SET
            sum_value=excluded.sum_value,min_value=excluded.min_value,max_value=excluded.max_value,valid_points=excluded.valid_points""",
                (target, target, target, cluster, source, left, right),
            )

    async def prune(self, now=None):
        now = int(time.time()) if now is None else now
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM resource_coarse_coverage WHERE ts<%s", (four_months_ago(now) // 3600 * 3600,)
            )
            await conn.execute(
                "DELETE FROM resource_history_coverage WHERE ts<%s", ((now - 2 * 86400) // 3600 * 3600,)
            )
            # Rollups are materialized in the same transaction as raw ingestion.
            for grain, cutoff in (
                (60, now - 2 * 86400),
                (300, now - 8 * 86400),
                (3600, four_months_ago(now)),
            ):
                await conn.execute(
                    "DELETE FROM resource_metric_bucket WHERE grain=%s AND ts<%s",
                    (grain, cutoff // 3600 * 3600),
                )
