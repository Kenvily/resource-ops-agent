"""Result-driven, deterministic investigation of allocated but sustained low-use devices."""

import asyncio
import time
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clients.prometheus import SourceError
from app.efficiency.reports import InvestigationStore
from app.efficiency.store import PROFILE, EfficiencyStore
from app.efficiency.sync import EfficiencySync
from app.models.resource import ClusterFilter


class EfficiencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cluster: ClusterFilter = "all"
    hours: Literal[1, 6, 24] = 1
    utilization_threshold: float = Field(default=10, ge=0, le=30)
    memory_threshold_mb: float = Field(default=1024, ge=1, le=8192)
    minimum_allocation_gb: float = Field(default=4, gt=0, le=1000)
    sustained_minutes: int = Field(default=30, ge=15, le=120, strict=True)


FACTORS = {"MB": 1e6, "MiB": 2**20, "bytes": 1}


def utilization_summary(request, start, end, markers, rows):
    """Host measurements stand independently of KSM, quota and container evidence."""
    expected = (end - start) // 60 + 1
    usable = [
        r
        for r in rows
        if start <= r[0] <= end
        and (m := markers.get(r[0]))
        and m[1] in ("healthy", "degraded")
        and m[1] == m[2]
        and m[5] == PROFILE
    ]
    usage = [r for r in usable if r[6] is not None and 0 <= r[6] <= 100]
    memory = [r[7] for r in usable if r[7] is not None]
    last = next((r for r in usable if r[0] == end), None)
    coverage = len(usage) / expected
    complete = coverage >= 0.9 and last is not None and last[6] is not None
    mean = sum(r[6] for r in usage) / len(usage) if complete else None
    allocated = last[4] if last is not None else None
    if mean is None or allocated is None or last is None or last[5] not in FACTORS:
        status = "insufficient"
    elif allocated <= 0:
        status = "unallocated"
    elif mean <= request.utilization_threshold:
        status = "low_utilization"
    else:
        status = "above_threshold"
    return {
        "screening_status": status,
        "usage_valid_points": len(usage),
        "usage_coverage": coverage,
        "current_allocated_raw": allocated,
        "mean_utilization_pct": mean,
        "max_utilization_pct": max((r[6] for r in usage), default=None) if complete else None,
        "mean_physical_memory_bytes": sum(memory) / len(memory) if memory else None,
    }


def investigate(cluster, request, start, end, coverage, devices, workloads):
    if len(devices) > 100000 or len(workloads) > 200000:
        raise SourceError("核查证据超过预算，请缩短观察窗口")
    expected = (end - start) // 60 + 1
    markers = {c[0]: c for c in coverage}
    groups, associations = defaultdict(list), defaultdict(list)
    for w in workloads:
        associations[w[:3]].append(w)
    for d in devices:
        groups[d[1:3]].append(d)
    results = []
    for (node, uuid), rows in groups.items():
        by_time = {r[0]: r for r in rows}
        valid, streak, best, low_points = 0, 0, 0, 0
        identities = previous_restarts = None
        rejected = set()
        latest_workloads = []
        latest_trusted = False
        for ts in range(start, end + 1, 60):
            d, marker = by_time.get(ts), markers.get(ts)
            if (
                not d
                or not marker
                or marker[1] not in ("healthy", "degraded")
                or marker[1] != marker[2]
                or marker[5] != PROFILE
                or not d[8]
            ):
                streak = 0
                identities = previous_restarts = None
                rejected.add("采样缺测、质量异常或最新复查未成功")
                continue
            factor = FACTORS.get(d[5])
            if factor is None or d[4] is None or d[6] is None or d[7] is None:
                streak = 0
                rejected.add("额度单位或实际使用指标未确认")
                continue
            if ts == end:
                latest_trusted = True
            valid += 1
            children = [w for w in associations[(ts, node, uuid)] if w[7] > 0]
            current_ids = tuple(sorted((w[3], w[4], w[5], w[6]) for w in children))
            current_restarts = tuple((w[6], w[10]) for w in sorted(children, key=lambda w: w[3:6]))
            life_ok = bool(children) and all(
                w[6] and w[8] and w[8] <= ts - 15 * 60 and w[9] is True and w[10] is not None and w[11]
                for w in children
            )
            if not life_ok:
                rejected.add("无已分配业务，或任务尚处启动期、非 Running、生命周期证据不足")
            changed = identities is not None and (
                identities != current_ids or previous_restarts != current_restarts
            )
            if changed:
                rejected.add("观察期间 Pod 身份、关联业务或重启计数发生变化")
            allocated = d[4] * factor >= request.minimum_allocation_gb * 1e9
            low = d[6] <= request.utilization_threshold and d[7] <= request.memory_threshold_mb * 1e6
            if low and allocated:
                low_points += 1
            if life_ok and allocated and low:
                streak = 1 if changed else streak + 1
            else:
                streak = 0
            best = max(best, streak)
            identities, previous_restarts = current_ids, current_restarts
            if ts == end:
                latest_workloads = [
                    {
                        "namespace": w[3],
                        "pod": w[4],
                        "container": w[5],
                        "pod_uid": w[6],
                        "allocated_raw": w[7],
                        "started_at": w[8],
                        "running": w[9],
                        "restart_count": w[10],
                        "verified": w[11],
                    }
                    for w in children
                ]
        coverage_ratio = valid / expected
        last = by_time.get(end)
        sustained = max(0, streak - 1)  # N consecutive sampled timestamps span N-1 intervals.
        if coverage_ratio < 0.9 or not last or not latest_trusted or valid == 0:
            classification = "insufficient"
        elif sustained >= request.sustained_minutes:
            classification = "review_candidate"
        elif last[4] * FACTORS[last[5]] < request.minimum_allocation_gb * 1e9:
            classification = "below_allocation_threshold"
        elif last[6] > request.utilization_threshold:
            classification = "active_compute"
        elif last[7] is not None and last[7] > request.memory_threshold_mb * 1e6:
            classification = "memory_resident"
        elif rejected:
            classification = "lifecycle_or_gaps"
        else:
            classification = "no_sustained_low_use"
        results.append(
            {
                "cluster": cluster,
                "node": node,
                "device_uuid": uuid,
                "classification": classification,
                "coverage": coverage_ratio,
                "valid_points": valid,
                "expected_points": expected,
                "sustained_minutes": sustained,
                "longest_observed_minutes": max(0, best - 1),
                "low_points": low_points,
                "current_allocated_raw": last[4] if last else None,
                "allocation_unit": last[5] if last else "unverified",
                "workloads": latest_workloads[:30],
                "workloads_truncated": len(latest_workloads) > 30,
                "limitations": sorted(rejected),
                **utilization_summary(request, start, end, markers, rows),
            }
        )
    rank = {
        "review_candidate": 0,
        "insufficient": 1,
        "lifecycle_or_gaps": 2,
        "memory_resident": 3,
        "active_compute": 4,
        "below_allocation_threshold": 5,
        "no_sustained_low_use": 6,
    }
    results.sort(
        key=lambda r: (rank[r["classification"]], -r["sustained_minutes"], r["node"], r["device_uuid"])
    )
    return results


class EfficiencyService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings
        self.store = EfficiencyStore(history.store) if history else None
        self.sync = EfficiencySync(history.resources, self.store) if history else None
        self.gate = asyncio.Semaphore(2)
        self.agent: Any = None
        self.reports = InvestigationStore(history.store, settings) if history else None

    @property
    def capabilities(self):
        return {
            "supports_agent": True,
            "model_ready": bool(self.agent and self.agent.provider.ready),
            "available": bool(self.store and self.settings.efficiency_enabled),
            "database_configured": self.store is not None,
            "collection_enabled": self.settings.efficiency_enabled,
            "sync_status": self.sync.status if self.sync else "disabled",
        }

    async def evaluate(self, request: EfficiencyRequest, now=None, bundles=None):
        if not self.store or not self.settings.efficiency_enabled:
            raise SourceError("低效占用核查采集尚未启用，请配置数据库和 EFFICIENCY_ENABLED")
        clusters = ["yz", "zw"] if request.cluster == "all" else [request.cluster]
        if not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准进行核查")
        if request.sustained_minutes >= request.hours * 60:
            raise SourceError("持续时长须小于观察窗口，以保留核查余量")
        now = int(time.time()) if now is None else now
        end = (now - 120) // 60 * 60
        start = end - request.hours * 3600
        reports, results = [], []
        for cluster in clusters:
            packet = await self.store.read(cluster, start, end, include_containers=bundles is not None)
            coverage, devices, workloads = packet[:3]
            items = investigate(cluster, request, start, end, coverage, devices, workloads)
            if bundles is not None:
                bundles[cluster] = (*packet, items)
            results.extend(items)
            reports.append(
                {
                    "cluster": cluster,
                    "collected_points": len(coverage),
                    "expected_points": request.hours * 60 + 1,
                    "latest_collection_at": float(max((c[3] for c in coverage), default=0)) or None,
                    "device_count": len(items),
                    "candidate_count": sum(i["classification"] == "review_candidate" for i in items),
                    "steps": [
                        {
                            "check": "数据覆盖与源端新鲜度",
                            "status": "complete"
                            if items and all(i["coverage"] == 1 for i in items)
                            else "insufficient",
                        },
                        {
                            "check": "已分配额度与整卡实际使用联合筛选",
                            "status": "complete" if any(i["valid_points"] for i in items) else "insufficient",
                        },
                        {
                            "check": "Pod UID、Running、启动期与重启核查",
                            "status": "complete"
                            if items
                            and all(
                                i["workloads"] and all(w["verified"] for w in i["workloads"]) for i in items
                            )
                            else "insufficient",
                        },
                    ],
                    "warnings": sorted({warning for c in coverage for warning in c[4]})[:10]
                    + ([] if devices else ["尚无可核查的本地使用历史，等待后台采集；未生成低效结论"]),
                }
            )
        results.sort(
            key=lambda r: (
                r["classification"] != "review_candidate",
                -r["sustained_minutes"],
                r["cluster"],
                r["node"],
                r["device_uuid"],
            )
        )
        return {
            "mode": self.settings.data_mode,
            "usage_source": {
                "profile": PROFILE,
                "utilization_metric": "DCGM_FI_DEV_GPU_UTIL",
                "physical_memory_metric": "DCGM_FI_DEV_FB_USED",
                "source_memory_unit": "MiB",
                "evidence_memory_unit": "bytes",
            },
            "database_read_at": time.time(),
            "start": start,
            "end": end,
            "request": request.model_dump(),
            "clusters": reports,
            "results": results[:100],
            "result_count": len(results),
            "truncated": len(results) > 100,
            "limitations": [
                "结论针对整卡持续低使用及关联业务，仅供人工核查；不判定浪费或建议自动释放。",
                "整卡利用率与物理显存不能分摊到单个 Pod；共享业务只列关联记录。",
                "Pod UID 来自 kube-state-metrics，额度按业务名称、节点和时刻关联；不宣称任务因果。",
                "Running 与启动时间不区分数据准备、检查点或间歇推理阶段，仍需业务确认。",
            ],
        }
