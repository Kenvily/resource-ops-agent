#!/usr/bin/env python3
"""Administrative bounded backfill; page requests remain database-only."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient
from app.core.config import Settings
from app.core.time_ranges import range_seconds
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.operations.host_coarse import fill_hours
from app.operations.host_samples import fill_points
from app.services.resources import ResourceService


def batch_count(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("批数需为1到4096之间的整数") from None
    if not 1 <= number <= 4096:
        raise argparse.ArgumentTypeError("批数需为1到4096之间的整数")
    return number


async def status(store, selected, start, now, strategy="sample"):
    left = (start + 3599) // 3600 * 3600
    right = (now - 120) // 3600 * 3600 - 3600
    result = []
    async with store.pool.connection() as conn:
        for cluster in selected:
            rows = await (
                await conn.execute(
                    """SELECT ts FROM host_metric_bucket
            WHERE cluster=%s AND grain=3600 AND ts BETWEEN %s AND %s GROUP BY ts
            HAVING min(valid_points)=60 AND count(DISTINCT metric)=6 AND count(*)=count(DISTINCT node)*6""",
                    (cluster, left, right),
                )
            ).fetchall()
            if strategy == "sample":
                rows = await (
                    await conn.execute(
                        "SELECT ts FROM host_point_coverage WHERE cluster=%s AND ts BETWEEN %s AND %s AND complete",
                        (cluster, left, right),
                    )
                ).fetchall()
            per_metric = await (
                await conn.execute(
                    """SELECT metric,count(*) FROM (
            SELECT metric,ts FROM host_metric_bucket WHERE cluster=%s AND grain=3600 AND ts BETWEEN %s AND %s
            GROUP BY metric,ts HAVING min(valid_points)=60) q GROUP BY metric""",
                    (cluster, left, right),
                )
            ).fetchall()
            expected = max(0, (right - left) // 3600 + 1)
            result.append(
                {
                    "cluster": cluster,
                    "expected_hours": expected,
                    "complete_hours": len(rows),
                    "complete_statistic_hours_by_metric": dict(per_metric),
                    "strategy": "hourly_point"
                    if strategy == "sample"
                    else "minute_statistics",
                    "pending_hours": max(0, expected - len(rows)),
                }
            )
    print(
        json.dumps(
            {
                "state": "complete"
                if all(r["pending_hours"] == 0 for r in result)
                else "partial",
                "coverage": result,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


async def main(args):
    settings = Settings()
    settings.prom_timeout_seconds = args.source_timeout
    selected = ["yz", "zw"] if args.cluster == "all" else [args.cluster]
    if not set(selected).issubset(settings.insight_allowed_clusters):
        raise ValueError("cluster not authorized")
    source = (
        MockPrometheusClient(settings)
        if settings.data_mode == "mock"
        else HttpPrometheusClient(settings)
    )
    if isinstance(source, HttpPrometheusClient):
        source.semaphore = asyncio.Semaphore(1)
    store = HistoryStore(settings, read_only=args.status)
    await store.open()
    try:
        await store.validate()
        history = HistoryService(ResourceService(source, settings), store)
        history.host_coarse_retry_seconds = 86400
        history.host_coarse_oldest_first = True
        now = int(time.time())
        start = now - range_seconds(args.window, now)
        if args.status:
            await status(store, selected, start, now, args.strategy)
            return
        for batch in range(args.batches):
            changed = 0
            busy = False
            for cluster in selected:
                # Dedicated historical lock: current snapshots continue while old data is backfilled.
                async with store.pool.connection() as conn:
                    locked = (
                        await (
                            await conn.execute(
                                "SELECT pg_try_advisory_lock(%s)",
                                (7500 if cluster == "yz" else 7501,),
                            )
                        ).fetchone()
                    )[0]
                    if not locked:
                        busy = True
                        print(
                            json.dumps({"cluster": cluster, "state": "busy"}),
                            flush=True,
                        )
                        continue
                    try:
                        hours = await (
                            fill_points if args.strategy == "sample" else fill_hours
                        )(history, cluster, start=start, budget=args.batch_hours)
                        changed += hours
                        print(
                            json.dumps(
                                {
                                    "batch": batch + 1,
                                    "cluster": cluster,
                                    "hours_attempted": hours,
                                    **history.host_coarse_report,
                                }
                            ),
                            flush=True,
                        )
                    finally:
                        await conn.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (7500 if cluster == "yz" else 7501,),
                        )
            if not changed:
                if busy:
                    await asyncio.sleep(1)
                    continue
                break
        await status(store, selected, start, int(time.time()), args.strategy)
    finally:
        await store.close()
        await source.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="只读查看完整小时覆盖，不访问上游"
    )
    parser.add_argument("--source-timeout", type=float, choices=[8, 15, 30], default=30)
    parser.add_argument("--window", choices=["1w", "1mo", "3mo"], default="3mo")
    parser.add_argument("--cluster", choices=["all", "yz", "zw"], default="all")
    parser.add_argument(
        "--strategy",
        choices=["sample", "aggregate"],
        default="sample",
        help="sample: 小时采样趋势；aggregate: 分钟采样生成完整小时统计",
    )
    parser.add_argument("--batch-hours", type=int, choices=range(1, 169), default=24)
    parser.add_argument("--batches", type=batch_count, metavar="N", default=3072)
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception:  # noqa: BLE001 - do not expose credentials or private source labels
        print(
            "主机历史回填失败，请检查数据库或上游状态；已提交批次保留，可重复执行。",
            file=sys.stderr,
        )
        raise SystemExit(1)
