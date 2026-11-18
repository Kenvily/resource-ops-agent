"""Administrative CLI. No SQL or synchronization write endpoint is exposed by the product."""

import argparse
import asyncio

from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient, SourceError
from app.core.config import Settings
from app.history.store import HistoryStore
from app.history.sync import HistorySync
from app.services.resources import ResourceService


async def execute(command, batches):
    settings = Settings()
    if not settings.history_database_url.get_secret_value():
        raise RuntimeError("请配置 HISTORY_DATABASE_URL")
    store = HistoryStore(settings)
    source = (
        MockPrometheusClient(settings) if settings.data_mode == "mock" else HttpPrometheusClient(settings)
    )
    try:
        await store.open()
        if command == "init":
            await store.initialize()
            print("历史数据库初始化成功")
        else:
            await store.validate()
            if command == "validate":
                await store.validate_constraints()
                print("存量数据库约束校验通过")
            elif command == "rollup":
                import time

                for _ in range(batches):
                    await store.refresh_rollups(int(time.time()))
                print("已有原始数据的统计补建批次完成；未触发 Prometheus 查询")
            elif command == "discover":
                sync = HistorySync(ResourceService(source, settings), store)
                from datetime import datetime
                from zoneinfo import ZoneInfo

                for cluster in ("yz", "zw"):
                    ts = await sync.discover_start(cluster, force=True)
                    print(
                        {
                            "cluster": cluster,
                            "earliest_visible": datetime.fromtimestamp(
                                ts, ZoneInfo("Asia/Shanghai")
                            ).isoformat(),
                        }
                    )
                print("已更新已发现起点；部分源返回仍须核验，不代表全量回填完成")
            elif command == "repair":
                await store.reset_progress()
                print("同步进度已重置，已有历史保留；请运行 backfill 重新核验缺口")
            elif command in ("sync", "backfill"):
                sync = HistorySync(ResourceService(source, settings), store)
                for _ in range(batches):
                    await sync.tick(budget=6 if command == "sync" else 60)
                    if sync.last_status != "ok":
                        raise SourceError("存在失败作用域或已有同步进程，请检查状态后重跑")
                print("同步批次完成；状态以持久化进度为准")
            else:
                print(await store.status())
    finally:
        await source.aclose()
        await store.close()


def main():
    parser = argparse.ArgumentParser(description="PostgreSQL 历史初始化和有界同步")
    parser.add_argument(
        "command", choices=("init", "sync", "backfill", "repair", "discover", "rollup", "status", "validate")
    )
    parser.add_argument("--batches", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.batches <= 100:
        parser.error("batches 必须为 1–100")
    try:
        asyncio.run(execute(args.command, args.batches))
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - sanitized CLI boundary
        # Connection errors can include credentials/hosts; CLI logs only a safe summary.
        parser.exit(1, "历史操作失败或已中断，请检查配置、数据库及同步进度；未完成批次可重跑。\n")


if __name__ == "__main__":
    main()
