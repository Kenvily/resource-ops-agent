"""Bounded, sanitized release evidence. PostgreSQL enforces read-only sessions."""

import asyncio
import math
import time
from functools import partial

from app.core.time_ranges import four_months_ago, range_seconds
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.operations.host_history import HostHistoryService
from app.resources.models import DeviceHistoryRequest, StatisticsRequest
from app.resources.service import GRAINS, ResourceStatisticsService
from app.services.resources import ResourceService
from app.workloads.service import WorkloadService


class NoExternal:
    calls = 0

    async def instant_query(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("验收工具禁止访问 Prometheus/模型")

    range_query = instant_query


def percentiles(values):
    ordered = sorted(values)
    return {f"p{p}_ms": round(ordered[math.ceil(len(ordered) * p / 100) - 1], 2) for p in (50, 95, 99)}


async def measure(operation, runs, concurrency):
    sem = asyncio.Semaphore(concurrency)
    times = []
    errors = 0

    async def once():
        nonlocal errors
        async with sem:
            start = time.perf_counter()
            try:
                async with asyncio.timeout(20):
                    await operation()
            except Exception:  # noqa: BLE001 - never output labels/SQL/credentials
                errors += 1
            times.append((time.perf_counter() - start) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(once() for _ in range(runs)))
    return {
        "runs": runs,
        "concurrency": concurrency,
        "errors": errors,
        **percentiles(times),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


async def collect(settings, *, runs=8, concurrency=2):
    if not 1 <= runs <= 30 or not 1 <= concurrency <= 4:
        raise ValueError("验收预算为1–30次、1–4并发")
    store = HistoryStore(settings, read_only=True)
    external = NoExternal()
    try:
        await store.open()
        await store.validate()
        history = HistoryService(ResourceService(external, settings), store)
        stats = ResourceStatisticsService(history, settings)
        workloads = WorkloadService(history, settings)
        # Only fixed templates and aggregate counts leave this process.
        now = int(time.time())
        async with store.pool.connection() as c:
            await c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            readonly = (await (await c.execute("SHOW transaction_read_only")).fetchone())[0]
            db_bytes = (await (await c.execute("SELECT pg_database_size(current_database())")).fetchone())[0]
            version = (await (await c.execute("SELECT version FROM history_metadata WHERE id=1")).fetchone())[
                0
            ]
            inventory = await (
                await c.execute("SELECT cluster,count(*) FROM resource_device GROUP BY cluster")
            ).fetchall()
            benchmark_device = await (
                await c.execute(
                    "SELECT cluster,node,device_uuid FROM resource_device ORDER BY last_seen DESC,cluster,node,device_uuid LIMIT 1"
                )
            ).fetchone()
            # Recorded source bounds are discovery evidence, not retention configuration proof.
            source_bounds = await (
                await c.execute("SELECT cluster,earliest_ts,discovery_complete FROM history_source_state")
            ).fetchall()
        initial = await stats.statistics(StatisticsRequest())
        current = [{"cluster": r["cluster"], "status": r["status"]} for r in initial["sources"]]
        counts = dict(inventory)
        coverage = []
        for name in ("1h", "1d", "1w", "1mo", "3mo", "retained"):
            grain = GRAINS.get(name, 3600)
            right = (now - 180) // grain * grain
            seconds = range_seconds(name, right) if name != "retained" else right - four_months_ago(right)
            left = math.ceil((right - seconds) / grain) * grain + grain
            async with store.pool.connection() as c:
                records = await (
                    await c.execute(
                        """SELECT cluster,count(DISTINCT ts),sum(valid_points)
                FROM resource_metric_bucket WHERE grain=%s AND metric='gpu_util' AND ts BETWEEN %s AND %s
                GROUP BY cluster""",
                        (grain, left, right),
                    )
                ).fetchall()
            async with store.pool.connection() as c:
                quota_rows = await (
                    await c.execute(
                        """SELECT cluster,count(*),count(*) FILTER(WHERE status='healthy' AND quality_version=2)
                    FROM history_coverage WHERE grain=%s AND ts BETWEEN %s AND %s GROUP BY cluster""",
                        (grain, left, right),
                    )
                ).fetchall()
            quotas = {r[0]: r[1:] for r in quota_rows}
            found = {r[0]: r[1:] for r in records}
            for cid in settings.insight_allowed_clusters:
                ticks, valid = found.get(cid, (0, 0))
                denominator = (right - left + grain) // 60 * counts.get(cid, 0)
                coverage.append(
                    {
                        "window": name,
                        "cluster": cid,
                        "grain_seconds": grain,
                        "start": left,
                        "end": right,
                        "observed_timestamps": ticks,
                        "expected_timestamps": (right - left) // grain + 1,
                        "quota_observed_timestamps": quotas.get(cid, (0, 0))[0],
                        "quota_healthy_timestamps": quotas.get(cid, (0, 0))[1],
                        "valid_device_minutes": int(valid or 0),
                        "registered_inventory_reference_minutes": denominator,
                        "registered_inventory_reference_ratio": round(int(valid or 0) / denominator, 6)
                        if denominator
                        else None,
                    }
                )
        workload = await workloads.read("all")
        benchmarks = []
        cases = [
            ("statistics", lambda: stats.statistics(StatisticsRequest())),
            ("workloads", lambda: workloads.read("all")),
        ]
        if benchmark_device:
            device = dict(zip(("cluster", "node", "device_uuid"), benchmark_device))
            for window in ("1h", "1d", "1w", "1mo", "3mo"):
                query = DeviceHistoryRequest(
                    cluster=device["cluster"],
                    node=device["node"],
                    device_uuid=device["device_uuid"],
                    window=window,
                )
                cases.append(("device_" + window, partial(stats.history_for_device, query)))
        for name, operation in cases:
            for parallel in sorted({1, concurrency}):
                benchmarks.append({"case": name, **await measure(operation, runs, parallel)})
        return {
            "observed_at": now,
            "mode": settings.data_mode,
            "schema_version": version,
            "transaction_read_only": readonly,
            "database_bytes": db_bytes,
            "registered_device_counts": counts,
            "current_observed_device_counts": {
                cid: sum(d["cluster"] == cid for d in initial["devices"])
                if any(s["cluster"] == cid and s["status"] in ("healthy", "degraded") for s in current)
                else None
                for cid in settings.insight_allowed_clusters
            },
            "current_sources": current,
            "source_discovery": [
                {"cluster": r[0], "earliest_recorded": r[1], "discovery_complete": r[2]}
                for r in source_bounds
            ],
            "gpu_utilization_coverage": coverage,
            "host_history": await HostHistoryService(history, settings).status(
                settings.insight_allowed_clusters, now
            ),
            "workloads": {
                "containers": len(workload["containers"]),
                "coverage": workload["coverage"],
                "sources": [{"cluster": s["cluster"], "status": s["status"]} for s in workload["sources"]],
            },
            "benchmarks": benchmarks,
            "external_calls": external.calls,
            "release_status": "pending_enterprise_acceptance",
            "deployment_checks": {
                "production_profile": settings.production_mode,
                "gateway_secret_configured": len(settings.gateway_shared_secret.get_secret_value()) >= 32,
                "human_writes_disabled": not settings.operations_workspace_enabled
                and not settings.investigation_feedback_enabled,
                "history_query_fill_disabled": settings.history_query_fill_chunks == 0,
                "verified_units": settings.memory_capacity_unit != "unverified"
                and settings.memory_allocation_unit != "unverified",
                "model_enabled": settings.llm_enabled,
                "model_processing_authorized": settings.llm_data_processing_authorized,
            },
            "limitations": [
                "设备分钟分母以累计登记设备为参考，不能证明历史设备生命周期或精确缺测比例。",
                "retained为四个月本地保留窗口；全部的源端起点另列，未重新探测源端。",
                "服务层短测不含HTTP、认证网关、模型和生产持续负载；未完成SSO、权限或业务结论验收。",
            ],
        }
    finally:
        await store.close()
