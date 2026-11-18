#!/usr/bin/env python3
"""Export de-identified, read-only database views to stdout for the local PDF renderer.

Requires explicit approval to use real data. Never synchronizes, invokes models or exports credentials.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analytics.service import AnalyticsService
from app.core.config import Settings
from app.history.service import HistoryService
from app.history.snapshots import SnapshotService
from app.history.store import HistoryStore
from app.insights.assessment import AssessmentRequest, AssessmentService
from app.investigations.service import InvestigationService
from app.operations.hosts import read_hosts
from app.operations.metrics import MetricsService, alerts
from app.resources.models import StatisticsRequest
from app.resources.service import ResourceStatisticsService
from app.resources.trends import FleetTrendRequest, fleet_trends
from app.services.resources import ResourceService


class NoSource:
    calls = 0

    async def instant_query(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("PDF export cannot contact monitoring sources")

    range_query = instant_query


class ReadOnlyRecords(InvestigationService):
    async def recover(self):
        # Export freezes existing states; it must not expire or recover tasks.
        return


def anonymize(value):
    mapping, counters = {}, {}
    private = {
        "node": "服务器",
        "node_name": "服务器",
        "device_uuid": "GPU",
        "uuid": "GPU",
        "namespace": "命名空间",
        "namespace_name": "命名空间",
        "pod": "负载",
        "pod_name": "负载",
        "container": "容器",
        "container_name": "容器",
        "model_key": "模型",
        "display_name": "业务模型",
        "category": "类别",
    }

    def register(raw, category):
        if not isinstance(raw, str) or not raw or raw in mapping:
            return
        counters[category] = counters.get(category, 0) + 1
        mapping[raw] = f"{category}-{counters[category]:03d}"

    def discover(item, path=""):
        if isinstance(item, dict):
            for key, val in item.items():
                if key in private:
                    register(val, private[key])
                if key == "name" and "business_models" in path:
                    register(val, "业务模型")
                if key == "name" and "machines" in path:
                    register(val, "服务器")
                if key == "label" and "entities" in path:
                    register(val, "调查对象")
                if key == "model_names" and isinstance(val, list):
                    for name in val:
                        register(name, "业务版本")
                if key == "key" and isinstance(val, list) and item.get("dimension"):
                    for part in val:
                        register(part, "调查对象")
                discover(val, path + "/" + key)
        elif isinstance(item, list):
            for val in item:
                discover(val, path)

    discover(value)
    # Longest first prevents a namespace prefix from hiding a full Pod match.
    pattern = (
        re.compile(
            "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
        )
        if mapping
        else None
    )
    uuids = {}

    def clean(text):
        if pattern:
            text = pattern.sub(lambda m: mapping[m.group()], text)
        text = text.replace("亦庄", "集群 A").replace("兆维", "集群 B")
        text = re.sub(r"https?://[^\s\"<>）)]+", "[源地址已隐藏]", text)
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[地址已隐藏]", text)

        def task_id(match):
            raw = match.group()
            if raw not in uuids:
                uuids[raw] = f"00000000-0000-4000-8000-{len(uuids) + 1:012d}"
            return uuids[raw]

        return re.sub(
            r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b", task_id, text
        )

    def walk(item):
        if isinstance(item, str):
            return clean(item)
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if key in ("attributes", "request_headers", "bindings"):
                    result[key] = {}
                elif key in ("question", "failure_reason"):
                    result[key] = "资源与数据质量调查（已保存记录，问题原文已隐藏）"
                elif key in ("description", "notes", "note"):
                    result[key] = "说明已隐藏"
                else:
                    result[key] = walk(val)
            return result
        return item

    return walk(value)


async def export():
    captured_at = time.time()
    original_clock = time.time
    configured = Settings()
    if (
        configured.data_mode != "live"
        or not configured.history_database_url.get_secret_value()
    ):
        raise RuntimeError(
            "real export requires the configured Live PostgreSQL database"
        )
    settings = configured.model_copy(
        update={
            "production_mode": True,
            "history_query_fill_chunks": 0,
            "llm_enabled": False,
        }
    )
    store = HistoryStore(settings, read_only=True)
    source = NoSource()
    try:
        await store.open()
        await store.validate()
        async with store.pool.connection() as conn:
            if (await (await conn.execute("SHOW transaction_read_only")).fetchone())[
                0
            ] != "on":
                raise RuntimeError("read-only database guard required")
        async with store.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT payload,last_good FROM resource_snapshot WHERE cluster=ANY(%s)",
                    (settings.insight_allowed_clusters,),
                )
            ).fetchall()
        stored_times = [
            p.get("fetched_at")
            for pair in rows
            for p in pair
            if isinstance(p, dict)
            and p.get("status") == "healthy"
            and p.get("fetched_at")
        ]
        if not stored_times:
            raise RuntimeError("no historical healthy snapshot available")
        source_as_of = max(stored_times)
        # Only this export process uses an explicit historical observation time.
        # Metrics themselves and all persisted sampling times remain unchanged.
        time.time = lambda: source_as_of + 1
        resource = ResourceService(source, settings)
        snapshots = SnapshotService(resource, store)
        history = HistoryService(resource, store)
        statistics = ResourceStatisticsService(history, settings)
        output = {
            "capture": {
                "source": "live_postgresql",
                "read_only": True,
                "captured_at": captured_at,
                "source_as_of": source_as_of,
                "historical_snapshot": True,
                "redacted": True,
            }
        }
        output["overview"] = (await snapshots.overview("all", False)).model_dump(
            mode="json"
        )
        output["statistics"] = await statistics.statistics(StatisticsRequest())
        metrics = await MetricsService(history, settings).read(["yz", "zw"])
        events = alerts(metrics, settings.operations_gpu_temperature_c)
        output["monitoring"] = {
            "hosts": await read_hosts(history, ["yz", "zw"]),
            "host_collection_enabled": settings.host_metrics_enabled,
            "metrics": metrics,
            "alerts": events[:200],
            "alert_count": len(events),
            "truncated": len(events) > 200,
            "mode": "live",
            "database_read_at": time.time(),
            "health": "attention" if events else "no_rule_triggered",
            "collection_enabled": settings.operations_metrics_enabled,
            "temperature_threshold_c": settings.operations_gpu_temperature_c,
        }
        # A current, explicit assessment: no reservation and no external/model call.
        output["assessment"] = await AssessmentService(
            snapshots, history, settings
        ).evaluate(AssessmentRequest(gpu_count=2, memory_per_gpu=20))
        output["trend"] = await fleet_trends(
            statistics,
            FleetTrendRequest(metric="gpu_util", window="1d", history_mode="auto"),
        )
        output["analytics"] = await AnalyticsService(history, settings).summary(7)
        records = ReadOnlyRecords(history, settings)
        output["tasks"] = await records.recent("all")
        output["investigation"] = None
        for row in output["tasks"].get("tasks", []):
            if row["status"] not in ("complete", "limited"):
                continue
            detail = await records.detail(row["id"], "all")
            if detail.get("result", {}).get("findings"):
                output["investigation"] = detail
                if detail["result"].get("answer_markdown"):
                    break
        for key in ("statistics", "assessment", "monitoring"):
            output[key]["database_read_at"] = captured_at
        for key in ("overview", "trend"):
            output[key]["fetched_at"] = captured_at
        if source.calls:
            raise RuntimeError("source access attempted")
        print(json.dumps(anonymize(output), ensure_ascii=False, allow_nan=False))
    finally:
        time.time = original_clock
        await store.close()


if __name__ == "__main__":
    if "--authorized-real-data" not in sys.argv:
        raise SystemExit("需明确授权后使用 --authorized-real-data 导出真实数据；输出仅供本地PDF渲染。")
    try:
        asyncio.run(export())
    except Exception:  # noqa: BLE001 - private export boundary
        raise SystemExit(
            "真实数据展示导出未完成；请检查本地Live数据库、版本及可查询数据。未输出凭证或业务标签。"
        ) from None
