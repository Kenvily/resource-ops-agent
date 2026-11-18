"""Database-only quota screening. No reservations, predictions or scheduler access."""

import asyncio
import math
import time
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.clients.prometheus import SourceError
from app.models.history_analysis import AnalysisWindow
from app.models.resource import ClusterFilter


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cluster: ClusterFilter = "all"
    gpu_count: int = Field(default=2, ge=1, le=64, strict=True)
    memory_per_gpu: float = Field(default=20, gt=0, le=1_000_000)
    memory_unit: Literal["MB", "GB", "GiB"] = "GB"
    same_node: bool = True
    node: str | None = Field(default=None, min_length=1, max_length=256)
    duration_hours: float = Field(default=8, ge=1, le=8760)
    history_hours: Literal[1, 24, 168] = 24

    @property
    def required_bytes(self):
        return self.memory_per_gpu * {"MB": 1e6, "GB": 1e9, "GiB": 2**30}[self.memory_unit]


def runtime_label(hours: float):
    for size, label in ((720, "个月（每月按 30 天）"), (168, "周"), (24, "天")):
        if hours % size == 0:
            return f"{hours / size:g} {label}"
    return f"{hours:g} 小时"


FACTORS = {"bytes": 1, "MB": 1e6, "MiB": 2**20, "GB": 1e9, "GiB": 2**30}


class AssessmentService:
    def __init__(self, snapshots, history, settings):
        self.snapshots, self.history, self.settings = snapshots, history, settings
        self.gate = asyncio.Semaphore(2)

    async def evaluate(self, request: AssessmentRequest, now=None):
        if not self.history:
            raise SourceError("部署评估需要已配置的 PostgreSQL 数据库")
        clusters = ["yz", "zw"] if request.cluster == "all" else [request.cluster]
        if not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准进行评估")
        now = time.time() if now is None else now
        grain: Literal[60, 300, 3600] = 60 if request.history_hours == 1 else 300
        end = int(now - 120) // grain * grain
        window = AnalysisWindow(start=end - request.history_hours * 3600, end=end, grain=grain)
        units = self.history.resources.units()
        factor = FACTORS.get(units.capacity) if units.unit_verified else None
        limitations = [
            "仅初筛 HAMi 虚拟显存额度；未接入配额、节点状态、亲和与 HAMi 调度条件，不能保证部署成功。",
            f"预计运行 {runtime_label(request.duration_hours)}不代表未来额度承诺；历史波动仅供核查。",
        ]
        if request.duration_hours > request.history_hours:
            limitations.append("预计运行时长超过所选历史观察窗口，历史证据不能覆盖同等时长。")
        reports, candidates = [], []
        # One committed database view for both current cluster snapshots; never synchronize here.
        async with self.history.store.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshots = {cid: await self.history.store.snapshot(cid, conn) for cid in clusters}
        read_at = time.time()
        for cid in clusters:
            summary = self.snapshots.present(cid, now, snapshots[cid], True)
            report = {
                "cluster": cid,
                "status": summary.status,
                "database_read_at": read_at,
                "source_sampled_at": summary.source_sampled_at,
                "snapshot_stored_at": summary.snapshot_stored_at,
                "eligible_gpu_count": 0,
                "excluded_gpu_count": 0,
                "warnings": list(summary.warnings),
            }
            reports.append(report)
            if summary.status != "healthy" or factor is None:
                report["warnings"].append("快照不完整、过期或单位未确认，未生成额度候选")
                continue
            gpus = [g for g in summary.gpus if request.node is None or g.node == request.node]
            if len(gpus) > 1000:
                report["warnings"].append("设备超过评估预算，请限定节点")
                continue
            histories = {}
            try:
                comparison = await self.history.analysis.gpu_history(cid, window, request.node)
                if comparison.truncated:
                    report["warnings"].append("历史实体超过预算，未生成历史风险排名")
                else:
                    histories = {tuple(c.entity): c.target for c in comparison.changes}
            except SourceError:
                report["warnings"].append("历史查询不可用，保留当前额度初筛并标记历史未知")
            eligible = []
            seen = set()
            for gpu in gpus:
                key = (gpu.node, gpu.device_uuid)
                if key in seen:
                    raise SourceError("设备标识重复，已停止评估")
                seen.add(key)
                capacity, allocated = gpu.capacity_raw, gpu.allocated_raw
                if (
                    gpu.warnings
                    or gpu.sampled_at is None
                    or now - gpu.sampled_at > self.settings.stale_after_seconds
                    or capacity is None
                    or allocated is None
                    or not all(math.isfinite(v) for v in (capacity, allocated))
                    or allocated < 0
                    or capacity <= 0
                    or allocated > capacity
                ):
                    report["excluded_gpu_count"] += 1
                    continue
                spare = (capacity - allocated) * factor
                if spare < request.required_bytes:
                    continue
                period = histories.get(key)
                historical = (
                    period.unallocated_min_raw * factor
                    if period and period.complete and period.unallocated_min_raw is not None
                    else None
                )
                risk = (
                    "unknown"
                    if historical is None
                    else "historical_shortfall"
                    if historical < request.required_bytes
                    else "within_observed"
                )
                eligible.append(
                    {
                        "cluster": cid,
                        "node": gpu.node,
                        "device_uuid": gpu.device_uuid,
                        "capacity_bytes": capacity * factor,
                        "allocated_bytes": allocated * factor,
                        "unallocated_bytes": spare,
                        "historical_min_bytes": historical,
                        "coverage": period.observed_points / period.expected_points if period else 0,
                        "expected_points": period.expected_points if period else window.expected_points,
                        "observed_points": period.observed_points if period else 0,
                        "risk": risk,
                    }
                )
            report["eligible_gpu_count"] = len(eligible)
            groups = defaultdict(list)
            for gpu in eligible:
                groups[gpu["node"] if request.same_node else "cluster"].append(gpu)
            for group in groups.values():
                if len(group) < request.gpu_count:
                    continue
                group.sort(
                    key=lambda g: (
                        g["risk"] != "within_observed",
                        g["risk"] == "unknown",
                        -(g["historical_min_bytes"] or 0),
                        -g["unallocated_bytes"],
                        g["node"],
                        g["device_uuid"],
                    )
                )
                selected = group[: request.gpu_count]
                risks = {g["risk"] for g in selected}
                risk = (
                    "unknown"
                    if "unknown" in risks
                    else ("historical_shortfall" if "historical_shortfall" in risks else "within_observed")
                )
                candidates.append(
                    {
                        "cluster": cid,
                        "nodes": sorted({g["node"] for g in selected}),
                        "gpus": selected,
                        "risk": risk,
                        "minimum_headroom_bytes": min(g["unallocated_bytes"] for g in selected)
                        - request.required_bytes,
                    }
                )
            if not groups or not any(len(group) >= request.gpu_count for group in groups.values()):
                report["warnings"].append("所选条件下当前额度不足以组成候选；不代表所有调度方案均不可行")
        candidates.sort(
            key=lambda c: (
                c["risk"] != "within_observed",
                c["risk"] == "unknown",
                -c["minimum_headroom_bytes"],
                c["cluster"],
                c["nodes"],
            )
        )
        return {
            "mode": self.settings.data_mode,
            "kind": "quota_screening",
            "request": request.model_dump(),
            "required_bytes_per_gpu": request.required_bytes,
            "history_window": window.model_dump(),
            "database_read_at": read_at,
            "clusters": reports,
            "candidates": candidates[:10],
            "candidate_count": len(candidates),
            "truncated": len(candidates) > 10,
            "limitations": limitations,
        }


router = APIRouter(prefix="/api/v1/insights", tags=["insights"])


@router.post("/assess")
async def assess(request: Request):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "评估仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "评估请求必须使用 application/json")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 4096:
            raise HTTPException(413, "评估请求过大")
    try:
        query = AssessmentRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "评估条件无效，请检查数量、单位、时间与节点") from None
    service = request.app.state.assessment
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准进行评估")
    if service.gate.locked():
        raise HTTPException(429, "评估并发已满，请稍后重试")
    async with service.gate:
        try:
            async with asyncio.timeout(20):
                return await service.evaluate(query)
        except SourceError as error:
            raise HTTPException(503, str(error)) from None
        except Exception:  # noqa: BLE001 - never expose SQL, private source or credentials
            raise HTTPException(503, "评估暂不可用，请检查数据库与同步状态") from None
