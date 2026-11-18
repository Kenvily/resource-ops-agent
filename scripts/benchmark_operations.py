"""Read-only service benchmark against the configured PostgreSQL, with upstream disabled.

PYTHONPATH=backend .venv/bin/python scripts/benchmark_operations.py --runs 20
Never initializes, seeds, prunes or refreshes a database. Output contains no business labels.
"""

import argparse
import asyncio
import json
import math
import time

from app.core.config import get_settings
from app.core.telemetry import Telemetry
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.operations.infrastructure import infrastructure
from app.operations.metrics import MetricsService
from app.operations.reports import ReportRequest, ReportService
from app.services.resources import ResourceService


class NoSource:
    calls = 0

    async def instant_query(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("benchmark must not contact Prometheus")

    range_query = instant_query


async def run(args):
    settings = get_settings()
    store = HistoryStore(settings)
    await store.open()
    try:
        await store.validate()
        source = NoSource()
        resources = ResourceService(source, settings)
        history = HistoryService(resources, store)
        metrics = MetricsService(history, settings)
        reports = ReportService(resources, history, metrics, settings)
        output = []
        for concurrency in (1, 4):
            for window in ("1d", "1w", "1mo"):
                durations = []
                semaphore = asyncio.Semaphore(concurrency)

                async def measure(
                    semaphore=semaphore, window=window, durations=durations
                ):
                    async with semaphore:
                        started = time.perf_counter()
                        result = await reports.generate(ReportRequest(window=window))
                        durations.append((time.perf_counter() - started) * 1000)
                        return sum(
                            bool(c["comparison"] and c["comparison"]["complete"])
                            for c in result["clusters"]
                        )

                complete = await asyncio.gather(*(measure() for _ in range(args.runs)))
                ordered = sorted(durations)
                output.append(
                    {
                        "window": window,
                        "concurrency": concurrency,
                        "runs": args.runs,
                        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.5) - 1], 2),
                        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
                        "last_complete_clusters": complete[-1],
                    }
                )
        evidence = await infrastructure(history, Telemetry())
        print(
            json.dumps(
                {
                    "kind": "read_only_service_benchmark",
                    "mode": settings.data_mode,
                    "results": output,
                    "prometheus_calls": source.calls,
                    "database_bytes": evidence["database"]["database_bytes"],
                    "limitation": "服务层实测，不含HTTP/模型；连接池4，未模拟长期写入增长或跨实例任务",
                },
                ensure_ascii=False,
            )
        )
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20, choices=range(5, 51))
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except Exception:  # noqa: BLE001 - never expose database credentials
        raise SystemExit(
            "只读基准未完成，请检查数据库配置、版本和范围；未更改数据"
        ) from None
