"""Mock-only low-use evidence benchmark; requires a disposable TEST_HISTORY_DATABASE_URL.

PYTHONPATH=backend TEST_HISTORY_DATABASE_URL=... .venv/bin/python scripts/benchmark_efficiency.py
Seeds one hour of synthetic evidence. Never initialize this against a Live database.
"""

import asyncio
import os
import statistics
import time

from app.clients.prometheus import MockPrometheusClient
from app.core.config import Settings
from app.efficiency.service import EfficiencyRequest, EfficiencyService
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.services.resources import ResourceService


async def main():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        raise SystemExit("请设置独立可丢弃的 Mock TEST_HISTORY_DATABASE_URL")
    config = Settings(
        _env_file=None,
        data_mode="mock",
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
        efficiency_enabled=True,
        history_database_url=url,
    )
    store = HistoryStore(config)
    source = MockPrometheusClient(config)
    try:
        await store.open()
        await (
            store.initialize()
        )  # Refuses non-Mock or mismatched existing contracts before writes.
        service = EfficiencyService(
            HistoryService(ResourceService(source, config), store), config
        )
        assert service.sync is not None
        now = int(time.time())
        end = (now - 120) // 60 * 60
        for cluster in ("yz", "zw"):
            for start in range(end - 3600, end + 1, 1800):
                await service.sync.ingest(cluster, start, min(start + 1740, end))

        async def forbidden(*args, **kwargs):
            raise AssertionError("核查期间不允许查询 Prometheus")

        source.instant_query = forbidden
        source.range_query = forbidden
        durations = []
        for _ in range(20):
            started = time.perf_counter()
            report = await service.evaluate(EfficiencyRequest(), now)
            durations.append((time.perf_counter() - started) * 1000)
        print(
            f"Mock dual-cluster 20 runs: p50={statistics.median(durations):.2f}ms "
            f"p95={sorted(durations)[18]:.2f}ms "
            f"investigated_devices={report['result_count']} Prometheus=0"
        )
    finally:
        await store.close()
        await source.aclose()


if __name__ == "__main__":
    asyncio.run(main())
