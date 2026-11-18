"""Atomic bounded minute facts, separate from HAMi virtual-memory history."""

from psycopg.types.json import Jsonb

PROFILE = "dcgm-percent-mib-to-bytes-ksm-uid-v2"


class EfficiencyStore:
    def __init__(self, history_store):
        self.pool = history_store.pool

    async def write(self, cluster, start, end, rows, failure=None, containers=()):
        by_time: dict[int, list[dict]] = {ts: [] for ts in range(start, end + 1, 60)}
        for row in rows:
            by_time[row["ts"]].append(row)
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (7300 if cluster == "yz" else 7301,))
            for ts, devices in by_time.items():
                status = (
                    "unavailable"
                    if failure
                    else "empty"
                    if not devices
                    else "healthy"
                    if all(d["verified"] for d in devices)
                    else "degraded"
                )
                previous = await (
                    await conn.execute(
                        "SELECT status,profile FROM efficiency_coverage WHERE cluster=%s AND ts=%s",
                        (cluster, ts),
                    )
                ).fetchone()
                # Preserve complete evidence on a failed/degraded recheck; expose latest failure separately.
                if previous == ("healthy", PROFILE) and status != "healthy":
                    await conn.execute(
                        "UPDATE efficiency_coverage SET last_attempt_status=%s,collected_at=now() WHERE cluster=%s AND ts=%s",
                        (status, cluster, ts),
                    )
                    continue
                await conn.execute(
                    "DELETE FROM efficiency_coverage WHERE cluster=%s AND ts=%s", (cluster, ts)
                )
                await conn.execute(
                    "INSERT INTO efficiency_coverage(cluster,ts,profile,status,warnings,last_attempt_status) VALUES(%s,%s,%s,%s,%s,%s)",
                    (cluster, ts, PROFILE, status, Jsonb([failure] if failure else []), status),
                )
                for device in devices:
                    await conn.execute(
                        """INSERT INTO efficiency_device_sample VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            cluster,
                            ts,
                            device["node"],
                            device["device_uuid"],
                            device["capacity_raw"],
                            device["allocated_raw"],
                            device["allocation_unit"],
                            device["utilization_pct"],
                            device["physical_memory_bytes"],
                            device["verified"],
                            Jsonb(device["warnings"]),
                        ),
                    )
                    for w in device["workloads"]:
                        await conn.execute(
                            """INSERT INTO efficiency_workload_sample VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                cluster,
                                ts,
                                device["node"],
                                device["device_uuid"],
                                w["namespace_name"],
                                w["pod_name"],
                                w["container_name"],
                                w["pod_uid"],
                                w["allocated_raw"],
                                w["pod_started_at"],
                                w["running"],
                                w["restart_count"],
                                w["verified"],
                                Jsonb(w["warnings"]),
                            ),
                        )

            for signal in containers:
                # A downgraded point may have preserved old parent facts; do not attach a new optional signal to it.
                parent = await (
                    await conn.execute(
                        "SELECT last_attempt_status=status FROM efficiency_coverage WHERE cluster=%s AND ts=%s",
                        (cluster, signal["ts"]),
                    )
                ).fetchone()
                if not parent or not parent[0]:
                    continue
                await conn.execute(
                    """INSERT INTO efficiency_container_sample VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(cluster,ts,node,device_uuid,namespace_name,pod_name,container_name) DO NOTHING""",
                    (
                        cluster,
                        signal["ts"],
                        signal["node"],
                        signal["device_uuid"],
                        signal["namespace_name"],
                        signal["pod_name"],
                        signal["container_name"],
                        signal["pod_uid"],
                        signal["device_model"],
                        signal["utilization_pct"],
                        signal["memory_bytes"],
                        signal["kernel_elapsed_seconds"],
                        signal["utilization_verified"],
                        Jsonb(signal["signal_warnings"]),
                    ),
                )

    async def container_evidence(self, cluster, start, end):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    """SELECT ts,node,device_uuid,namespace_name,pod_name,container_name,
                pod_uid,device_model,utilization_pct,memory_bytes,kernel_elapsed_seconds,utilization_verified,warnings
                FROM efficiency_container_sample WHERE cluster=%s AND ts BETWEEN %s AND %s
                ORDER BY ts,node,device_uuid,namespace_name,pod_name,container_name LIMIT 200001""",
                    (cluster, start, end),
                )
            ).fetchall()

    async def read(self, cluster, start, end, include_containers=False):
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            coverage = await (
                await conn.execute(
                    "SELECT ts,status,last_attempt_status,extract(epoch from collected_at),warnings,profile FROM efficiency_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s ORDER BY ts",
                    (cluster, start, end),
                )
            ).fetchall()
            devices = await (
                await conn.execute(
                    """SELECT d.ts,d.node,d.device_uuid,d.capacity_raw,d.allocated_raw,
                d.allocation_unit,d.utilization_pct,d.physical_memory_bytes,d.verified,d.warnings
                FROM efficiency_device_sample d WHERE d.cluster=%s AND d.ts BETWEEN %s AND %s
                ORDER BY d.node,d.device_uuid,d.ts LIMIT 100001""",
                    (cluster, start, end),
                )
            ).fetchall()
            workloads = await (
                await conn.execute(
                    """SELECT ts,node,device_uuid,namespace_name,pod_name,
                container_name,pod_uid,allocated_raw,pod_started_at,running,restart_count,verified
                FROM efficiency_workload_sample WHERE cluster=%s AND ts BETWEEN %s AND %s
                ORDER BY ts,node,device_uuid,namespace_name,pod_name,container_name LIMIT 200001""",
                    (cluster, start, end),
                )
            ).fetchall()
            if include_containers:
                signals = await (
                    await conn.execute(
                        """SELECT ts,node,device_uuid,namespace_name,pod_name,container_name,
                    pod_uid,device_model,utilization_pct,memory_bytes,kernel_elapsed_seconds,utilization_verified,warnings
                    FROM efficiency_container_sample WHERE cluster=%s AND ts BETWEEN %s AND %s
                    ORDER BY ts,node,device_uuid,namespace_name,pod_name,container_name LIMIT 200001""",
                        (cluster, start, end),
                    )
                ).fetchall()
                return coverage, devices, workloads, signals
            return coverage, devices, workloads

    async def missing(self, cluster, start, end):
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT ts FROM efficiency_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s AND profile=%s AND (((status='healthy' AND last_attempt_status='healthy') AND (NOT EXISTS(SELECT 1 FROM efficiency_workload_sample w WHERE w.cluster=efficiency_coverage.cluster AND w.ts=efficiency_coverage.ts) OR EXISTS(SELECT 1 FROM efficiency_container_sample s WHERE s.cluster=efficiency_coverage.cluster AND s.ts=efficiency_coverage.ts))) OR collected_at>now()-interval '10 minutes')",
                    (cluster, start, end, PROFILE),
                )
            ).fetchall()
        return set(range(start, end + 1, 60)) - {r[0] for r in rows}

    async def prune(self, now):
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM efficiency_coverage WHERE ts<%s", (now - 2 * 86400,))
            await conn.execute(
                "DELETE FROM efficiency_investigation WHERE created_at<now()-interval '90 days'"
            )
