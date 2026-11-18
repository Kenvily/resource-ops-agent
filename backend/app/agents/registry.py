"""Composed read-only registry: reuse domain tools, keep identities on the server."""

import copy
import json
import time
from typing import Any

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

from app.agents.contracts import provenance
from app.clients.prometheus import SourceError
from app.efficiency.agent import LABELS as USAGE_LABELS
from app.efficiency.agent import UsageRegistry, usage_schemas
from app.efficiency.service import EfficiencyRequest
from app.history.contracts import HistoryScope
from app.insights.models import DiagnosisRequest
from app.insights.tools import ToolRegistry
from app.resources.models import StatisticsRequest
from app.resources.trends import FleetTrendRequest, fleet_trends


class ResourceRegistry:
    def __init__(self, service, query, emit, clock=None, focus=None):
        self.service, self.request, self.emit = service, query, emit
        self.clusters = ["yz", "zw"] if query.cluster == "all" else [query.cluster]
        self.evidence, self.cache, self.entities = [], {}, {}
        self.calls, self.model_calls, self.output_bytes = 0, 0, 0
        self.clock = time.time() if clock is None else clock
        self.diagnosis = ToolRegistry(
            service.history,
            DiagnosisRequest(cluster=query.cluster, window=query.window, question="跨资源查证"),
            service.settings,
            self.capture,
            int(self.clock),
        )
        self.windows = self.diagnosis.windows
        self.usage = None
        self.focus = focus
        if focus and focus["cluster"] not in self.clusters:
            raise SourceError("调查对象超出任务范围")
        if focus and focus["dimension"] in ("gpu", "node"):
            scope = HistoryScope()
            self.diagnosis._entity(focus["cluster"], focus["dimension"], tuple(focus["key"]), scope, "full")

    async def capture(self, event):
        if event["type"] != "evidence":
            return
        item = copy.deepcopy(event["data"])
        data = item["data"]
        meta = item.get("provenance", {})
        # Only explicit observed times, never an attempted-at or requested-end substitute.
        latest = (
            data.get("statistics_latest_at")
            or data.get("hourly_instant_latest_at")
            or data.get("data_as_of")
            or meta.get("data_as_of")
        )
        if item["tool"] == "get_cluster_trend":
            latest = max((p["timestamp"] for p in data.get("points", []) if p.get("valid")), default=None)
        if item["tool"] == "inspect_gpu_usage" and data.get("mean_utilization_pct") is not None:
            latest = data.get("end")
        meta.update(data_as_of=latest, queried_at=time.time(), grain=data.get("grain"))
        item["provenance"] = meta
        item.setdefault("status", "limited")
        size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode())
        if size > 32000 or self.output_bytes + size > 180000:
            raise SourceError("跨资源证据超过响应预算")
        self.output_bytes += size
        self.evidence.append(item)
        await self.emit({"type": "evidence", "data": item})

    def schemas(self):
        output = list(self.diagnosis.schemas())
        if self.request.window in ("1h", "1d"):
            output.extend(usage_schemas(self.clusters))
        from app.agents.verification import KINDS

        for name, label, properties, required in (
            (
                "assess_hypothesis",
                "按引用证据校验假设，不能使用模型自报数字",
                {
                    "cluster": {"type": "string", "enum": self.clusters},
                    "hypothesis": {"type": "string", "enum": list(KINDS)},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
                },
                ["cluster", "hypothesis", "evidence_ids"],
            ),
            (
                "get_resource_snapshot",
                "读取当前资源总数与匿名GPU/主机统计",
                {"cluster": {"type": "string", "enum": self.clusters}},
                ["cluster"],
            ),
            (
                "get_resource_history",
                "读取GPU实际利用率、物理显存、CPU、系统内存或负载的数据库趋势；缺测不补零",
                {
                    "cluster": {"type": "string", "enum": self.clusters},
                    "metric": {
                        "type": "string",
                        "enum": [
                            "gpu_util",
                            "gpu_memory",
                            "cpu_utilization_pct",
                            "memory_used_bytes",
                            "load1",
                        ],
                    },
                },
                ["cluster", "metric"],
            ),
        ):
            output.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": label,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": properties,
                            "required": required,
                        },
                    },
                }
            )
        return output

    async def prepare_usage(self):
        if self.usage:
            return
        hours = {"1h": 1, "1d": 24}.get(self.request.window)
        if not hours:
            raise SourceError("整卡持续性核查仅有近1小时/1天的完整细粒度基础；不能用小时采样推断持续低效")
        bundles: dict[str, Any] = {}
        request = EfficiencyRequest(cluster=self.request.cluster, hours=hours)
        report = await self.service.efficiency.evaluate(request, now=self.clock, bundles=bundles)
        if self.focus:
            report["results"] = [
                r
                for r in report["results"]
                if r["cluster"] == self.focus["cluster"]
                and r["node"] == self.focus["key"][0]
                and (self.focus["dimension"] == "node" or r["device_uuid"] == self.focus["key"][1])
            ]
        self.usage = UsageRegistry(
            self.service.efficiency,
            request,
            report,
            bundles,
            {cid: packet[3] for cid, packet in bundles.items()},
            self.capture,
        )

    async def call(self, name, arguments):
        schema = next(
            (s["function"]["parameters"] for s in self.schemas() if s["function"]["name"] == name), None
        )
        if (
            not schema
            or not isinstance(arguments, dict)
            or set(arguments) - set(schema["properties"])
            or set(schema.get("required", [])) - set(arguments)
        ):
            raise SourceError("工具或参数未授权")
        if arguments.get("cluster", self.clusters[0]) not in self.clusters:
            raise SourceError("工具集群越界")
        canonical = arguments
        try:
            if name in {t["function"]["name"] for t in self.diagnosis.schemas()}:
                from app.insights.tools import Arguments as DiagnosisArguments

                canonical = DiagnosisArguments.model_validate(arguments).model_dump(
                    mode="json", include=set(schema["properties"])
                )
            elif name in USAGE_LABELS:
                from app.efficiency.agent import Arguments as UsageArguments

                canonical = UsageArguments.model_validate(arguments).model_dump(
                    mode="json", include=set(schema["properties"])
                )
        except ValidationError:
            raise SourceError("工具参数无效") from None
        key = (name, json.dumps(canonical, sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        if self.calls >= self.service.settings.insight_max_tool_calls:
            raise SourceError("工具次数达到预算")
        self.calls += 1
        before = len(self.evidence)
        returned = None
        try:
            if name in USAGE_LABELS:
                await self.prepare_usage()
                assert self.usage is not None
                returned = await self.usage.call(name, arguments)
            elif name in {s["function"]["name"] for s in self.diagnosis.schemas()}:
                if self.focus and name in (
                    "compare_cluster_windows",
                    "check_data_quality",
                    "check_host_history_coverage",
                ):
                    await self.focused(name, arguments)
                elif self.focus and name == "get_cluster_trend":
                    raise SourceError("对象趋势须使用get_resource_history")
                else:
                    bounded = dict(arguments)
                    if self.focus and name == "rank_allocation_changes":
                        bounded.setdefault("parent_entity_id", next(iter(self.diagnosis.entities)))
                    returned = await self.diagnosis.call(name, bounded)
            else:
                await self.custom(name, arguments)
        except (SourceError, DatabaseError, PoolTimeout, OSError):
            # A failed attempt is still evidence; never convert it into a zero measurement.
            await self.add(
                name, arguments, {"message": "数据不可用、范围不支持或达到预算；不能据此作资源结论"}, "error"
            )
        if returned is not None:
            proof_id = returned.id if hasattr(returned, "id") else returned["id"]
            item = next((e for e in self.evidence if e["id"] == proof_id), None)
            if item is None:
                raise SourceError("工具返回的证据未登记")
            if len(self.evidence) == before:
                self.calls -= 1
        else:
            item = self.evidence[-1]
        self.cache[key] = item
        return item

    async def custom(self, name, args):
        if name == "assess_hypothesis":
            from app.agents.verification import assess

            refs = args["evidence_ids"]
            if not isinstance(refs, list) or len(refs) > 16 or not all(isinstance(r, str) for r in refs):
                raise SourceError("引用参数无效")
            proofs = [e for e in self.evidence if e["id"] in refs]
            if len({e["id"] for e in proofs}) != len(set(refs)) or any(
                e.get("cluster") not in (None, args["cluster"]) for e in proofs
            ):
                raise SourceError("引用未知或越界证据")
            verdict = assess(args["hypothesis"], proofs, args["cluster"])
            await self.add(
                name,
                args,
                verdict,
                "limited" if verdict["state"] in ("unknown", "conflicting") else "complete",
            )
        elif name == "get_resource_snapshot":
            report = await self.service.resources.statistics(StatisticsRequest(cluster=args["cluster"]))
            if self.focus:
                from app.resources.service import aggregate

                report["devices"] = [
                    r
                    for r in report["devices"]
                    if r["cluster"] == self.focus["cluster"]
                    and r["node"] == self.focus["key"][0]
                    and (self.focus["dimension"] == "node" or r["device_uuid"] == self.focus["key"][1])
                ]
                report["summary"] = aggregate(report["devices"])
                report["servers"] = [
                    r
                    for r in report["servers"]
                    if r["cluster"] == self.focus["cluster"] and r["node"] == self.focus["key"][0]
                ]
            devices = []
            for i, row in enumerate(report["devices"][:100]):
                ref = self.diagnosis._entity(
                    row["cluster"], "gpu", (row["node"], row["device_uuid"]), HistoryScope(), "full"
                )
                self.entities[ref] = {
                    "cluster": row["cluster"],
                    "dimension": "gpu",
                    "key": [row["node"], row["device_uuid"]],
                }
                devices.append(
                    {
                        "entity_id": ref,
                        "quota_sampled_at": row.get("quota_sampled_at"),
                        "usage_sampled_at": row.get("usage_sampled_at"),
                        "temperature_sampled_at": row.get("temperature_sampled_at"),
                        **{
                            k: row.get(k)
                            for k in (
                                "gpu_model",
                                "allocated_raw",
                                "hami_capacity_raw",
                                "utilization_pct",
                                "physical_used_mib",
                                "temperature_c",
                            )
                        },
                    }
                )
            hosts = []
            for row in report["servers"][:100]:
                ref = self.diagnosis._entity(row["cluster"], "node", (row["node"],), HistoryScope(), "full")
                host = row.get("host") or {}
                hosts.append(
                    {
                        "entity_id": ref,
                        "status": host.get("status", "unknown"),
                        "node_ready": row.get("node_ready"),
                        "evaluated_at": host.get("evaluated_at"),
                        **{
                            k: host.get(k)
                            for k in (
                                "cpu_cores",
                                "server_model",
                                "cpu_utilization_pct",
                                "memory_total_bytes",
                                "memory_used_bytes",
                                "load1",
                            )
                        },
                    }
                )
            data = {
                "devices_truncated": len(report["devices"]) > 100,
                "hosts_truncated": len(report["servers"]) > 100,
                "physical_memory_observed_gpus": sum(
                    r.get("physical_used_mib") is not None for r in report["devices"]
                ),
                "physical_memory_zero_gpus": sum(r.get("physical_used_mib") == 0 for r in report["devices"]),
                "hosts": hosts,
                "summary": report["summary"],
                "sources": report["sources"],
                "devices": devices,
                "units": report["units"],
                "database_read_at": report["database_read_at"],
                "complete": report["complete"],
            }
            await self.add(name, args, data, "complete" if report["complete"] else "limited")
        else:
            if args["metric"] not in (
                "gpu_util",
                "gpu_memory",
                "cpu_utilization_pct",
                "memory_used_bytes",
                "load1",
            ):
                raise SourceError("指标未授权")
            report = await self.history_report(args)
            points = report["series"][0]["points"]
            valid = [p for p in points if p["value"] is not None]
            # Bounded temporal summaries, not large point arrays or fabricated hourly extrema.
            data = {
                "metric": args["metric"],
                "start": report["start"],
                "end": report["end"],
                "grain": report["step"],
                "sampling": report["sampling"],
                "observed_points": len(valid),
                "expected_points": len(points),
                "data_as_of": max((p["timestamp"] for p in valid), default=None),
                "observed_min": min((p["value"] for p in valid), default=None),
                "observed_max": max((p["value"] for p in valid), default=None),
                "first_value": valid[0]["value"] if valid else None,
                "last_value": valid[-1]["value"] if valid else None,
                "unit": {
                    "gpu_util": "%",
                    "gpu_memory": "MiB",
                    "cpu_utilization_pct": "%",
                    "memory_used_bytes": "bytes",
                    "load1": "load",
                }[args["metric"]],
                "limitation": "仅描述已观测时点，不证明完整时段峰值、持续性或因果关系",
            }
            await self.add(name, args, data, "complete" if report["complete"] else "limited")

    async def add(self, name, args, data, status):
        pair = self.windows["full"]
        meta = provenance(
            name,
            args,
            data,
            cluster=args.get("cluster"),
            start=data.get("start", pair["baseline"].start) if name != "get_resource_snapshot" else None,
            end=data.get("end", pair["target"].end) if name != "get_resource_snapshot" else None,
            units=data.get("units", {"unit": data.get("unit")}),
            status=status,
        )
        meta["data_as_of"] = data.get("data_as_of")
        if name == "get_resource_snapshot":
            # Mixed snapshot metrics carry their individual sample times; no single time is fabricated.
            meta["data_as_of"] = None
        if name == "assess_hypothesis":
            meta["source"] = "derived_verifier"
        item = {
            "id": f"resource-evidence-{len(self.evidence) + 1}",
            "tool": name,
            "label": name,
            "arguments": args,
            "cluster": args.get("cluster"),
            "status": status,
            "data": data,
            "provenance": meta,
        }
        await self.capture({"type": "evidence", "data": item})

    def bindings(self):
        result = dict(self.entities)
        for eid, entity in self.diagnosis.entities.items():
            result[eid] = {"cluster": entity.cluster, "dimension": entity.dimension, "key": list(entity.key)}
        if self.usage:
            for eid, row in self.usage.entities.items():
                result[eid] = {
                    "cluster": row["cluster"],
                    "dimension": "gpu",
                    "key": [row["node"], row["device_uuid"]],
                }
        return result

    async def focused(self, name, args):
        pair = self.windows[args.get("segment", "full")]
        cluster = self.focus["cluster"]
        if name == "check_host_history_coverage":
            from app.operations.history_quality import HistoryQualityService

            data = await HistoryQualityService(self.service.history, self.service.settings).coverage(
                cluster,
                args.get("host_metric", "cpu_utilization_pct"),
                pair["baseline"].start,
                pair["target"].end,
                pair["baseline"].grain,
                node=self.focus["key"][0],
            )
            data["scope"] = "selected_host"
            await self.add(name, args, data, "limited")
        else:
            result = await self.service.history.analysis.compare_windows(
                cluster, **pair, dimension=self.focus["dimension"], entity=tuple(self.focus["key"])
            )
            data = self.diagnosis._comparison(result, args.get("segment", "full"))
            data["scope"] = "selected_entity"
            await self.add(name, args, data, "complete" if result.complete else "limited")

    async def history_report(self, args):
        if not self.focus:
            return await fleet_trends(
                self.service.resources,
                FleetTrendRequest(
                    cluster=args["cluster"],
                    window=self.request.window,
                    metric=args["metric"],
                    history_mode="auto",
                ),
            )
        metric = args["metric"]
        if metric in ("gpu_util", "gpu_memory"):
            from app.resources.models import DeviceHistoryRequest

            if self.focus["dimension"] != "gpu" or self.request.window == "all":
                raise SourceError("请选定GPU及有界时间范围")
            data = await self.service.resources.history_for_device(
                DeviceHistoryRequest(
                    cluster=args["cluster"],
                    node=self.focus["key"][0],
                    device_uuid=self.focus["key"][1],
                    window=self.request.window,
                )
            )
            points = next(s["points"] for s in data["series"] if s["metric"] == metric)
            sampling = "minute_statistics"
        else:
            from app.operations.host_history import HostHistoryService, HostRequest

            report = await HostHistoryService(self.service.history, self.service.settings).detail(
                HostRequest(
                    cluster=args["cluster"],
                    node=self.focus["key"][0],
                    window=self.request.window,
                    history_mode="auto",
                )
            )
            data = report["history"]
            points = next(s["points"] for s in data["series"] if s["metric"] == metric)
            sampling = data["sampling"]
        normalized = [
            {
                "timestamp": p["timestamp"],
                "value": p.get("sampled_value") if p.get("sampled_value") is not None else p["mean"],
            }
            for p in points
        ]
        return {
            "start": data["start"],
            "end": data["end"],
            "step": data["grain"],
            "sampling": sampling,
            "complete": bool(normalized) and all(p["value"] is not None for p in normalized),
            "series": [{"points": normalized}],
        }


class DiagnosisPolicyView:
    """Adapt the existing typed diagnosis policy to the composed session without duplicating it."""

    def __init__(self, session):
        self.session = session
        self.diagnosis = session.registry.diagnosis

    def __getattr__(self, name):
        return getattr(self.diagnosis, name)

    async def call(self, name, arguments):
        from app.insights.models import Evidence

        return Evidence.model_validate(await self.session.call(name, arguments))
