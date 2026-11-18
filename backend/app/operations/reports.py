"""Manual, deterministic operational reports built from the shared database services."""

import asyncio
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.clients.prometheus import SourceError
from app.models.history_analysis import AnalysisWindow
from app.models.resource import ClusterFilter
from app.operations.metrics import alerts


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: ClusterFilter = "all"
    window: Literal["1d", "1w", "1mo"] = "1d"


def windows(window, now):
    seconds = {"1d": 86400, "1w": 7 * 86400, "1mo": 30 * 86400}[window]
    grain: Literal[300, 3600] = 300 if window == "1d" else 3600
    # Non-overlapping exact grid windows. No adjacent-period endpoint counted twice.
    end = (now - 120) // grain * grain
    target = AnalysisWindow(start=end - seconds + grain, end=end, grain=grain)
    baseline = AnalysisWindow(start=target.start - seconds, end=target.start - grain, grain=grain)
    return baseline, target


class ReportService:
    def __init__(self, resources, history, metrics, settings):
        self.resources, self.history, self.metrics, self.settings = resources, history, metrics, settings
        self.gate = asyncio.Semaphore(2)

    async def generate(self, query):
        if not self.history:
            raise SourceError("运营报告需要 PostgreSQL 历史数据库")
        clusters = ["yz", "zw"] if query.cluster == "all" else [query.cluster]
        if not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准查询")
        baseline, target = windows(query.window, int(time.time()))
        from app.history.snapshots import SnapshotService

        snapshots = SnapshotService(self.history.resources, self.history.store)
        result = []
        for cid in clusters:
            entry: dict[str, Any] = {
                "cluster": cid,
                "comparison": None,
                "namespace_changes": None,
                "warnings": [],
            }
            for name, method in (
                ("comparison", self.history.analysis.compare_windows),
                ("namespace_changes", self.history.analysis.allocation_changes),
            ):
                try:
                    evidence = await method(cid, baseline, target)
                    entry[name] = evidence.model_dump(mode="json")
                except SourceError:
                    entry["warnings"].append("历史证据不可用，未生成对应统计")
            c = snapshots.present(cid, time.time(), await self.history.store.snapshot(cid), True)
            entry["current"] = (
                {
                    "status": c.status,
                    "sampled_at": c.sampled_at,
                    "gpu_count": c.gpu_count_observed if c.status in ("healthy", "degraded") else None,
                    "allocated_raw": c.allocated_raw if c.status in ("healthy", "degraded") else None,
                    "capacity_raw": c.capacity_raw if c.status in ("healthy", "degraded") else None,
                }
                if c
                else None
            )
            result.append(entry)
        metrics = await self.metrics.read(clusters)
        current_alerts = alerts(metrics, self.settings.operations_gpu_temperature_c)
        return {
            "mode": self.settings.data_mode,
            "generated_at": time.time(),
            "window": query.window,
            "baseline": baseline.model_dump(),
            "target": target.model_dump(),
            "units": self.history.resources.units().model_dump(),
            "clusters": result,
            "metrics": metrics,
            "alerts": current_alerts[:200],
            "alert_count": len(current_alerts),
            "review_status": "draft",
            "calculation": "deterministic",
            "notes": [
                "时段统计与前一等长区间比较；缺测不生成增长结论。",
                "报告由数据库事实生成，需人工核对后导出。",
                "告警与指标为当前快照，不代表报告时段内的历史事件或平均利用率。",
            ],
        }
