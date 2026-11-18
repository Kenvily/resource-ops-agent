"""Administrative usage collection; no resource writes or arbitrary query arguments."""

import argparse
import asyncio

from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient
from app.core.config import get_settings
from app.efficiency.service import EfficiencyService
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.services.resources import ResourceService


async def run(args):
    settings = get_settings()
    if not settings.history_database_url.get_secret_value():
        raise RuntimeError("请先配置历史数据库")
    store = HistoryStore(settings)
    source = (
        MockPrometheusClient(settings) if settings.data_mode == "mock" else HttpPrometheusClient(settings)
    )
    try:
        await store.open()
        await store.validate()
        service = EfficiencyService(HistoryService(ResourceService(source, settings), store), settings)
        if args.command == "sync":
            assert service.sync is not None
            if not settings.efficiency_enabled:
                raise RuntimeError("请先启用 EFFICIENCY_ENABLED")
            for batch in range(args.batches):
                await service.sync.tick()
                print(f"batch={batch + 1} status={service.sync.status}")
                # tick obeys shared 55-second cooldown, even from another CLI/application process.
                if batch + 1 < args.batches:
                    await asyncio.sleep(60)
        async with store.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT cluster,count(*),min(ts),max(ts),count(*) FILTER(WHERE status='healthy' AND last_attempt_status='healthy') FROM efficiency_coverage GROUP BY cluster ORDER BY cluster"
                )
            ).fetchall()
        for cluster, count, start, end, healthy in rows:
            print(f"cluster={cluster} points={count} healthy={healthy} first={start} last={end}")
        if not rows:
            print("尚无使用指标采集记录")
        async with store.pool.connection() as conn:
            signals = await (
                await conn.execute(
                    "SELECT cluster,count(*),count(utilization_pct),count(memory_bytes),count(kernel_elapsed_seconds),count(*) FILTER(WHERE utilization_verified) FROM efficiency_container_sample WHERE ts>extract(epoch from now())-86400 GROUP BY cluster ORDER BY cluster"
                )
            ).fetchall()
        for cluster, total, util, memory, kernel, verified in signals:
            print(
                f"container cluster={cluster} points={total} utilization={util} memory={memory} kernel={kernel} verified={verified}"
            )
    finally:
        await store.close()
        await source.aclose()


def main():
    parser = argparse.ArgumentParser(description="低效占用核查采集维护")
    parser.add_argument("command", choices=("sync", "status"))
    parser.add_argument("--batches", type=int, default=1, choices=range(1, 25))
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:  # noqa: BLE001 - never log source URLs or database exceptions
        raise SystemExit("使用指标维护未完成，请检查数据库版本、配置及源端指标") from None


if __name__ == "__main__":
    main()
