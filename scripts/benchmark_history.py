"""Reproducible local benchmark. Uses only Mock labels; requires a disposable TEST database.

PYTHONPATH=backend TEST_HISTORY_DATABASE_URL=... .venv/bin/python scripts/benchmark_history.py
Optional BENCHMARK_LATENCY_MS (default 50): simulated per-Prometheus-request latency.
Results are NOT live Prometheus or production performance measurements.
"""

import asyncio
import json
import os
import statistics
import time
from types import SimpleNamespace

import app.services.trends as trend_module
from app.clients.prometheus import MockPrometheusClient
from app.core.config import Settings
from app.history.contracts import HistoryScope
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.models.history_analysis import AnalysisWindow
from app.services.resources import ResourceService
from app.services.trends import TrendService


class TimedSource(MockPrometheusClient):
    calls = 0

    async def range_query(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(float(os.getenv("BENCHMARK_LATENCY_MS", "50")) / 1000)
        return await super().range_query(*args, **kwargs)


def timing(values):
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(values) * 1000, 2),
        "p95_ms": round(ordered[max(0, int(len(values) * 0.95) - 1)] * 1000, 2),
    }


async def main():
    config = Settings(
        _env_file=None,
        data_mode="mock",
        mock_scenario="normal",
        prom_yz_url="",
        prom_zw_url="",
        history_database_url=os.environ["TEST_HISTORY_DATABASE_URL"],
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
        history_query_fill_chunks=0,
    )
    original_clock = trend_module.time
    fixed_now = int(time.time())
    trend_module.time = SimpleNamespace(time=lambda: fixed_now)
    store = HistoryStore(config)
    await store.open()
    try:
        await store.initialize()
        await store.prune(fixed_now)
        source = TimedSource(config)
        resources = ResourceService(source, config)
        history = HistoryService(resources, store)
        sql = TrendService(resources, history)
        prom = TrendService(resources)
        output = {
            "source": "Mock",
            "simulated_request_latency_ms": float(
                os.getenv("BENCHMARK_LATENCY_MS", "50")
            ),
            "iterations": 20,
            "windows": {},
        }
        # Seed complete grids for both clusters and all dimensions; preserve existing test rows.
        seeder = HistoryService(
            ResourceService(MockPrometheusClient(config), config), store
        )
        for window in ("1h", "24h", "7d", "4mo"):
            plan = await sql.query("all", window, refresh=True)
            grain = {"1h": 60, "24h": 300, "7d": 300, "4mo": 3600}[window]
            for cluster in ("yz", "zw"):
                for ts in range(plan.start, plan.end + 1, plan.step):
                    existing = await store.read(cluster, grain, ts, ts, grain)
                    if not existing or existing[0][3] != "healthy":
                        await seeder.sync.ingest(cluster, grain, ts, ts)
            durations = {}
            for name, service in (("prometheus_simulated", prom), ("postgresql", sql)):
                source.calls = 0
                values = []
                for _ in range(20):
                    started = time.perf_counter()
                    result = await service.query("all", window, refresh=True)
                    assert result.complete
                    values.append(time.perf_counter() - started)
                durations[name] = {
                    **timing(values),
                    "upstream_calls": source.calls,
                    "points_per_cluster": len(result.series[0].points),
                }
            output["windows"][window] = durations

        # Controlled analysis cases are database-only, even when a source client is present.
        async def forbidden(*args, **kwargs):
            raise AssertionError("analysis must not query Prometheus")

        source.range_query = source.instant_query = forbidden
        end = (fixed_now - 180) // 300 * 300
        a = AnalysisWindow(start=end - 7500, end=end - 3900, grain=300)
        b = AnalysisWindow(start=end - 3600, end=end, grain=300)
        gpu = source.fixture["yz"][0]
        scope = HistoryScope(node_name=gpu["node"])
        namespace = gpu["allocations"][0]["namespace"]
        cases = {
            "cluster_compare": lambda: history.analysis.compare_windows("yz", a, b),
            "namespace_changes": lambda: history.analysis.allocation_changes(
                "yz", a, b
            ),
            "node_to_namespace": lambda: history.analysis.allocation_changes(
                "yz", a, b, "namespace", scope
            ),
            "namespace_to_pod": lambda: history.analysis.allocation_changes(
                "yz", a, b, "pod", HistoryScope(namespace_name=namespace)
            ),
            "gpu_history": lambda: history.analysis.compare_windows(
                "yz", a, b, "gpu", (gpu["node"], gpu["uuid"])
            ),
        }
        output["analysis_cases"] = {}
        for name, call in cases.items():
            durations = []
            for _ in range(30):
                began = time.perf_counter()
                result = await call()
                assert result.complete
                durations.append(time.perf_counter() - began)
            output["analysis_cases"][name] = {**timing(durations), "upstream_calls": 0}
        async with store.pool.connection() as conn:
            await conn.execute("ANALYZE history_sample")
            await conn.execute("ANALYZE history_entity")
            await conn.execute("ANALYZE history_coverage")
            row = await (
                await conn.execute("""SELECT count(*),pg_total_relation_size('history_sample')+
                pg_total_relation_size('history_entity')+pg_total_relation_size('history_coverage') FROM history_sample""")
            ).fetchone()
            output["stored_rows"], output["table_and_index_bytes"] = row
            plan = await (
                await conn.execute(
                    """EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT s.ts,s.capacity,s.allocated FROM history_entity e JOIN history_sample s ON s.entity_id=e.id
                WHERE e.cluster='yz' AND e.dimension='cluster' AND e.entity_key='{}' AND s.grain=3600
                AND s.ts BETWEEN %s AND %s ORDER BY s.ts""",
                    (plan.start, plan.end),
                )
            ).fetchone()
            output["query_plan"] = plan[0]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        trend_module.time = original_clock
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
