"""Request-scoped, typed, database-only tool registry. Entity handles cannot escape a run."""

import itertools
import math
import time
from typing import Literal

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import Field, ValidationError

from app.agents.contracts import provenance
from app.clients.prometheus import SourceError
from app.core.time_ranges import four_months_ago, range_seconds
from app.history.contracts import HistoryScope
from app.insights.models import DiagnosisRequest, EntityReference, Evidence, EvidenceStatus, ToolArguments
from app.models.history_analysis import AnalysisWindow

LABELS = {
    "get_metric_definition": "确认指标口径",
    "check_data_quality": "核查历史覆盖与新鲜度",
    "get_cluster_trend": "定位主要变化区间",
    "compare_cluster_windows": "对比容量与分配",
    "rank_allocation_changes": "追查历史变化贡献",
    "inspect_resource_entity": "复核实体与关联范围",
    "check_host_history_coverage": "核查主机历史统计与小时采样覆盖",
    "inspect_host_sync_status": "核查主机回填检查点",
}


class Arguments(ToolArguments):
    host_metric: Literal[
        "cpu_utilization_pct",
        "cpu_cores",
        "memory_total_bytes",
        "memory_used_bytes",
        "memory_available_bytes",
        "load1",
    ] = "cpu_utilization_pct"
    metric: Literal["allocation", "capacity"] = "allocation"
    top_k: int = Field(default=3, ge=1, le=5)


def windows_for(request: DiagnosisRequest, now: int, timeout: int, earliest=None):
    grains: dict[str, Literal[60, 300, 3600]] = {
        "1h": 60,
        "1d": 300,
        "24h": 300,
        "1w": 300,
        "7d": 300,
        "1mo": 3600,
        "3mo": 3600,
        "all": 3600,
        "4mo": 3600,
    }
    grain = grains[request.window]
    end = (now - 180) // grain * grain
    seconds = range_seconds(request.window, end)
    if request.window == "all":
        seconds = end - (earliest if earliest is not None else four_months_ago(now))
    start = (
        math.ceil((four_months_ago(now) + timeout) / grain) * grain
        if seconds is None
        else end - seconds + grain
    )
    if request.window == "all":
        start = math.ceil((earliest if earliest is not None else four_months_ago(now)) / grain) * grain
    if request.start is not None and request.end is not None:
        if request.end > now or request.start < four_months_ago(now) + timeout:
            raise SourceError("诊断时间须在最近四个自然月内，且不能位于未来")
        grain = (
            60
            if request.start >= now - 2 * 86400 + timeout
            else 300
            if request.start >= now - 8 * 86400 + timeout
            else 3600
        )
        start = math.ceil(request.start / grain) * grain
        end = min(request.end, now - 180) // grain * grain
    analysis_step = max(grain, math.ceil((end - start + grain) / 4096 / grain) * grain)
    end = start + (end - start) // analysis_step * analysis_step
    count = (end - start) // grain + 1
    if count < 8:
        raise SourceError("所选范围过短，请至少覆盖8个有效采样间隔")
    count = (end - start) // analysis_step + 1
    middle = start + (count // 2) * analysis_step

    def pair(left, right):
        n = (right - left) // analysis_step + 1
        split = left + (n // 2) * analysis_step
        return {
            "baseline": AnalysisWindow(
                start=left, end=split - analysis_step, grain=grain, step=analysis_step
            ),
            "target": AnalysisWindow(start=split, end=right, grain=grain, step=analysis_step),
        }

    return {"full": pair(start, end), "early": pair(start, middle - analysis_step), "late": pair(middle, end)}


class ToolRegistry:
    def __init__(self, history, request, settings, emit, now=None, earliest=None):
        self.history, self.request, self.settings, self.emit = history, request, settings, emit
        self.clusters = ["yz", "zw"] if request.cluster == "all" else [request.cluster]
        if not set(self.clusters).issubset(settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准进行诊断")
        self.windows = windows_for(
            request, int(time.time()) if now is None else now, settings.insight_timeout_seconds, earliest
        )
        self.evidence = []
        self.entities = {}
        self.contexts = {}
        self.cache = {}
        self.output_bytes = 0
        self.calls = 0
        self.model_calls = 0

    def schemas(self):
        descriptions = {
            "check_host_history_coverage": "检查CPU/系统内存/负载的数据库历史覆盖，区分完整区间统计与小时采样；不能证明源端缺失。",
            "inspect_host_sync_status": "读取本范围内已持久化的主机回填检查点，不访问Prometheus，不推断后台任务是否正在运行。",
            "get_metric_definition": "读取HAMi口径与限制。",
            "check_data_quality": "检查前后半窗口覆盖、新鲜度和未知数据。先调用。",
            "get_cluster_trend": "只读数据库趋势，定位变化在early还是late。",
            "compare_cluster_windows": "比较同范围前后半段；full/early/late均由服务器限定。",
            "rank_allocation_changes": "按历史变化排名。业务按allocation，容量按capacity；祖先必须用已返回实体ID。",
            "inspect_resource_entity": "用已返回实体ID核查，不能输入任意标签。",
        }
        schema = Arguments.model_json_schema()
        allowed = {
            "check_host_history_coverage": ("cluster", "segment", "host_metric"),
            "inspect_host_sync_status": ("cluster", "segment"),
            "get_metric_definition": (),
            "check_data_quality": ("cluster", "segment"),
            "get_cluster_trend": ("cluster", "segment"),
            "compare_cluster_windows": ("cluster", "segment"),
            "rank_allocation_changes": (
                "cluster",
                "segment",
                "dimension",
                "parent_entity_id",
                "metric",
                "top_k",
            ),
            "inspect_resource_entity": ("cluster", "entity_id"),
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {k: schema["properties"][k] for k in allowed[name]},
                        "required": (["cluster"] if name != "get_metric_definition" else [])
                        + (
                            ["entity_id"]
                            if name == "inspect_resource_entity"
                            else ["dimension"]
                            if name == "rank_allocation_changes"
                            else []
                        ),
                    },
                },
            }
            for name, desc in descriptions.items()
        ]

    def _entity(self, cluster, dimension, key, scope, segment):
        identity = (cluster, dimension, tuple(key), scope.model_dump_json(), segment)
        for eid, ctx in self.contexts.items():
            if ctx[0] == identity:
                return eid
        eid = f"entity-{len(self.entities) + 1}"
        label = (
            (" / ".join(key) or ("亦庄" if cluster == "yz" else "兆维"))
            if dimension != "gpu"
            else key[0] + " · GPU " + str(len(self.entities) + 1)
        )
        self.entities[eid] = EntityReference(
            id=eid, cluster=cluster, dimension=dimension, key=tuple(key), label=label
        )
        self.contexts[eid] = (identity, scope, segment)
        return eid

    def _scope(self, cluster, args):
        if not args.parent_entity_id:
            return HistoryScope()
        ref = self.entities.get(args.parent_entity_id)
        if not ref or ref.cluster != cluster:
            raise SourceError("实体不属于本次授权范围")
        inherited = self.contexts[ref.id][1].model_dump(exclude_none=True)
        allowed = {"node": ("gpu", "namespace", "pod"), "gpu": ("namespace", "pod"), "namespace": ("pod",)}
        if args.dimension not in allowed.get(ref.dimension, ()):
            raise SourceError("不支持该层级下钻")
        if ref.dimension == "node":
            inherited["node_name"] = ref.key[0]
        elif ref.dimension == "gpu":
            inherited.update(node_name=ref.key[0], device_uuid=ref.key[1])
        elif ref.dimension == "namespace":
            inherited["namespace_name"] = ref.key[0]
        return HistoryScope(**inherited)

    def _comparison(self, result, segment):
        data = result.model_dump(mode="json")
        data.pop("scope", None)
        data["segment"] = segment
        for row, original in zip(data["changes"], result.changes):
            eid = self._entity(result.cluster, result.dimension, original.entity, result.scope, segment)
            row.pop("entity", None)
            row["entity_id"] = eid
        return data

    async def call(self, name, raw):
        start = time.perf_counter()
        if name not in LABELS:
            raise SourceError("不允许的工具")
        try:
            args = Arguments.model_validate(raw)
        except ValidationError:
            raise SourceError("工具参数无效") from None
        cache_key = (name, args.model_dump_json())
        if cache_key in self.cache:
            return self.cache[cache_key]
        allowed = next(
            t["function"]["parameters"]["properties"] for t in self.schemas() if t["function"]["name"] == name
        )
        if set(raw) - set(allowed):
            raise SourceError("该工具不支持这些参数")
        cluster = args.cluster
        if name != "get_metric_definition" and cluster not in self.clusters:
            raise SourceError("工具不能访问所选范围之外的集群")
        if self.calls >= self.settings.insight_max_tool_calls:
            raise SourceError("诊断工具次数达到预算")
        self.calls += 1
        pair = self.windows[args.segment]
        data = {}
        status: EvidenceStatus = "complete"
        try:
            if name == "get_metric_definition":
                data = {
                    "metric": "HAMi虚拟显存分配额度",
                    "not_measured": ["实际GPU利用率", "物理空闲显存", "调度能力"],
                    "units": self.history.resources.units().model_dump(),
                    "limitations": ["名称关联不证明Pod生命周期或因果关系", "未分配额度不能保证部署成功"],
                }
            elif name in ("check_host_history_coverage", "inspect_host_sync_status"):
                from app.operations.history_quality import HistoryQualityService

                quality = HistoryQualityService(self.history, self.settings)
                first, last = pair["baseline"].start, pair["target"].end
                if name == "check_host_history_coverage":
                    data = await quality.coverage(
                        cluster, args.host_metric, first, last, pair["baseline"].grain
                    )
                else:
                    data = await quality.sync_status(cluster, first, last)
                # These tools describe an observed DB scope, not source-retention verification.
                status = "limited"
            elif name == "check_data_quality":
                result = await self.history.analysis.compare_windows(cluster, **pair)
                row = result.changes[0]
                data = {
                    "complete": result.complete,
                    "baseline": row.baseline.model_dump(),
                    "target": row.target.model_dump(),
                    "warnings": result.warnings,
                }
                status = "complete" if result.complete else "limited"
            elif name == "get_cluster_trend":
                first, last = pair["baseline"].start, pair["target"].end
                step = (
                    math.ceil(((last - first) // pair["baseline"].grain + 1) / 121) * pair["baseline"].grain
                )
                aligned_end = first + ((last - first) // step) * step
                rows, _markers = await self.history.read_committed(
                    cluster, pair["baseline"].grain, first, aligned_end, step
                )
                points = [
                    {
                        "timestamp": r[0],
                        "capacity_raw": r[1],
                        "allocated_raw": r[2],
                        "valid": r[3] == "healthy",
                    }
                    for r in rows
                ]
                changes = [
                    (abs(b["allocated_raw"] - a["allocated_raw"]), b["timestamp"])
                    for a, b in itertools.pairwise(points)
                    if a["valid"]
                    and b["valid"]
                    and a["allocated_raw"] is not None
                    and b["allocated_raw"] is not None
                ]
                peak = max(changes, default=(0, None))
                data = {
                    "points": points,
                    "largest_observed_step_at": peak[1],
                    "suggested_segment": "late" if peak[1] and peak[1] >= pair["target"].start else "early",
                    "note": "降采样定位线索，不证明短时峰值或因果",
                }
                status = (
                    "complete"
                    if len(points) == (aligned_end - first) // step + 1 and all(p["valid"] for p in points)
                    else "limited"
                )
            elif name == "compare_cluster_windows":
                result = await self.history.analysis.compare_windows(cluster, **pair)
                data = self._comparison(result, args.segment)
                status = "complete" if result.complete else "limited"
            elif name == "rank_allocation_changes":
                scope = self._scope(cluster, args)
                if args.metric == "capacity" and args.dimension not in ("node", "gpu"):
                    raise SourceError("业务维度没有容量排名")
                result = await self.history.analysis.allocation_changes(
                    cluster,
                    **pair,
                    dimension=args.dimension,
                    scope=scope,
                    top_k=100 if args.metric == "capacity" else args.top_k,
                )
                if args.metric == "capacity":
                    if result.omitted_entities or result.truncated:
                        raise SourceError("容量候选超过排序预算，请收窄范围")
                    result.changes = sorted(
                        result.changes,
                        key=lambda x: (
                            not x.target.complete or not x.baseline.complete,
                            abs(x.capacity_change_raw or 0),
                        ),
                        reverse=True,
                    )[: args.top_k]
                    result.omitted_entities = (result.entity_count or 0) - len(result.changes)
                data = self._comparison(result, args.segment)
                data["metric"] = args.metric
                data["parent_entity_id"] = args.parent_entity_id
                status = "complete" if result.complete else "limited"
            else:
                ref = self.entities.get(args.entity_id)
                if not ref or ref.cluster != cluster:
                    raise SourceError("未知或越界的实体ID")
                _, scope, segment = self.contexts[ref.id]
                result = await self.history.analysis.compare_windows(
                    cluster, **self.windows[segment], dimension=ref.dimension, entity=ref.key, scope=scope
                )
                data = self._comparison(result, segment)
                status = "complete" if result.complete else "limited"
        except SourceError as reason:
            status = "error"
            data = {"message": str(reason)}
        except (DatabaseError, PoolTimeout, OSError):
            status = "error"
            data = {"message": "历史数据库暂不可用或查询预算超限"}
        eid = f"evidence-{len(self.evidence) + 1}"
        ev = Evidence(
            id=eid,
            tool=name,
            arguments=args.model_dump(mode="json"),
            label=LABELS[name],
            cluster=cluster,
            status=status,
            data=data,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        ev.data.setdefault("segment", args.segment)
        ev.provenance = provenance(
            name,
            ev.arguments,
            ev.data,
            cluster=cluster,
            start=pair["baseline"].start,
            end=pair["target"].end,
            units=(
                {"metric": args.host_metric, "unit": data.get("unit")}
                if name == "check_host_history_coverage"
                else {}
                if name == "inspect_host_sync_status"
                else self.history.resources.units().model_dump()
            ),
            status=status,
        )
        size = len(ev.model_dump_json().encode())
        if size > 32000 or self.output_bytes + size > 180000:
            raise SourceError("诊断证据响应超过预算")
        self.output_bytes += size
        self.cache[cache_key] = ev
        self.evidence.append(ev)
        await self.emit({"type": "evidence", "data": ev.model_dump(mode="json")})
        return ev

    def for_model(self, evidence):
        # Raw entity keys and scope labels stay server-side; the model sees opaque handles.
        return evidence.model_dump(mode="json")
