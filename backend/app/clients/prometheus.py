import asyncio
import json
import math
import time
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import CLUSTERS, Settings
from app.core.telemetry import Telemetry
from app.core.time_ranges import four_months_ago
from app.queries.hami import RangeTemplate, Template, query_for, range_query_for


class SourceError(Exception):
    """Safe, public error description; never includes upstream content or URL."""


class PrometheusClient(Protocol):
    async def instant_query(self, cluster: str, template: Template, at: float) -> dict: ...
    async def range_query(
        self,
        cluster: str,
        template: RangeTemplate,
        start: int,
        end: int,
        step: int,
        device: tuple[str, str] | None = None,
    ) -> dict: ...


class HttpPrometheusClient:
    def __init__(self, settings: Settings, transport=None, telemetry: Telemetry | None = None):
        self.settings = settings
        self.telemetry = telemetry or Telemetry()
        self.semaphore = asyncio.Semaphore(6)
        self.http = httpx.AsyncClient(
            timeout=settings.prom_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.origins = {"yz": settings.prom_yz_url.rstrip("/"), "zw": settings.prom_zw_url.rstrip("/")}

    async def aclose(self):
        await self.http.aclose()

    async def instant_query(self, cluster: str, template: Template, at: float) -> dict:
        return await self._request(
            cluster, template, {"query": query_for(cluster, template), "time": str(at)}, False
        )

    async def range_query(
        self,
        cluster: str,
        template: RangeTemplate,
        start: int,
        end: int,
        step: int,
        device: tuple[str, str] | None = None,
    ) -> dict:
        if (
            step < 1
            or end < start
            or (template not in ("first_seen", "source_probe") and start < four_months_ago(end))
            or (end - start) // step + 1 > self.settings.range_max_points
        ):
            raise SourceError("历史查询范围或数据点超出限制")
        query = range_query_for(cluster, template, self.settings.allocation_absence_is_zero, device)
        return await self._request(
            cluster, template, {"query": query, "start": str(start), "end": str(end), "step": str(step)}, True
        )

    async def _request(self, cluster: str, template: str, params: dict, is_range: bool) -> dict:
        started = time.monotonic()
        outcome = "ok"
        operation = ("range/" if is_range else "instant/") + template
        active = False
        self.telemetry.begin("upstream_outstanding")
        try:
            async with self.semaphore:
                self.telemetry.begin("upstream_active")
                active = True
                try:
                    async with asyncio.timeout(self.settings.prom_timeout_seconds):
                        endpoint = "/api/v1/query_range" if is_range else "/api/v1/query"
                        async with self.http.stream(
                            "GET", self.origins[cluster] + endpoint, params=params
                        ) as response:
                            if response.status_code in (401, 403):
                                raise SourceError("数据源认证失败，请联系管理员")
                            if response.status_code != 200:
                                raise SourceError(f"数据源 HTTP 错误（{response.status_code}）")
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk)
                                self.telemetry.upstream_bytes += len(chunk)
                                if len(chunks) > self.settings.prom_max_response_bytes:
                                    raise SourceError("数据源响应超过大小限制")
                            try:
                                payload = json.loads(chunks)
                            except (ValueError, UnicodeError):
                                raise SourceError("数据源返回非 JSON 响应") from None
                except (httpx.TimeoutException, TimeoutError):
                    outcome = "timeout"
                    raise SourceError("数据源查询超时") from None
                except httpx.RequestError:
                    raise SourceError("数据源连接失败") from None
            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise SourceError("数据源查询失败")
            data = payload.get("data")
            if (
                not isinstance(data, dict)
                or data.get("resultType") != ("matrix" if is_range else "vector")
                or not isinstance(data.get("result"), list)
            ):
                raise SourceError("数据源结果格式异常")
            if len(data["result"]) > (
                1
                if is_range and template in ("capacity", "allocated", "source_age")
                else self.settings.prom_max_series
            ):
                raise SourceError("数据源序列数超过限制")
            if is_range and any(
                not isinstance(row, dict)
                or not isinstance(row.get("values"), list)
                or len(row["values"]) > self.settings.range_max_points
                for row in data["result"]
            ):
                raise SourceError("历史查询数据点超出限制或格式异常")
            return payload
        except SourceError:
            if outcome != "timeout":
                outcome = "error"
            raise
        finally:
            if active:
                self.telemetry.end("upstream_active")
            self.telemetry.end("upstream_outstanding")
            self.telemetry.record("prometheus", cluster, operation, time.monotonic() - started, outcome)


class MockPrometheusClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.fixture = json.loads((Path(__file__).parents[1] / "fixtures/demo.json").read_text())

    async def aclose(self):
        pass

    async def instant_query(self, cluster: str, template: Template, at: float) -> dict:
        scenario = self.settings.mock_scenario
        if scenario == "partial" and cluster == "zw":
            raise SourceError("演示：数据源查询超时")
        if template == "freshness":
            return {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": []
                    if scenario == "empty"
                    else [{"metric": {}, "value": [at, str(600 if scenario == "stale" else 2)]}],
                },
            }
        timestamp = at - (600 if scenario == "stale" else 2)
        result = []
        for index, gpu in enumerate(self.fixture[cluster]):
            labels = {"cluster": CLUSTERS[cluster][1], "node": gpu["node"], "device_uuid": gpu["uuid"]}
            if template == "capacity":
                result.append({"metric": labels, "value": [timestamp, str(gpu["capacity"])]})
            else:
                if scenario == "missing" and index == 0:
                    continue
                amounts = gpu["allocations"]
                if scenario == "overallocated" and index == 0:
                    amounts = [dict(amounts[0], value=gpu["capacity"] + 2000)]
                if template == "allocated":
                    result.append(
                        {"metric": labels, "value": [timestamp, str(sum(a["value"] for a in amounts))]}
                    )
                else:
                    for allocation in amounts:
                        result.append(
                            {
                                "metric": dict(
                                    labels,
                                    namespace_name=allocation["namespace"],
                                    pod_name=allocation["pod"],
                                    container_name="worker",
                                    namespace="exporter-system",
                                    pod="exporter",
                                ),
                                "value": [timestamp, str(allocation["value"])],
                            }
                        )
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [] if scenario == "empty" else result},
        }

    async def range_query(
        self,
        cluster: str,
        template: RangeTemplate,
        start: int,
        end: int,
        step: int,
        device: tuple[str, str] | None = None,
    ) -> dict:
        if template.startswith("wl_"):
            from app.workloads.service import mock

            return mock(self.fixture, cluster, template, start, end, step)
        if template.startswith("host_"):
            from app.operations.hosts import mock_hosts

            return mock_hosts(self.fixture, cluster, template, start, end, step)
        if template.startswith("res_"):
            from app.resources.mock import coarse_payload

            return coarse_payload(self.fixture, cluster, template, start, end, step)
        if template.startswith("ops_"):
            from app.operations.mock import payload as operations_payload

            return operations_payload(self.fixture, cluster, template, start, end, step)
        if template.startswith("eff_"):
            from app.efficiency.mock import efficiency_range

            return efficiency_range(self.fixture, self.settings, cluster, template, start, end, step)
        if template in ("device_capacity", "device_allocated", "workload_allocated"):
            names: dict[str, Template] = {
                "device_capacity": "capacity",
                "device_allocated": "allocated",
                "workload_allocated": "workloads",
            }
            rows: dict[str, dict] = {}
            for timestamp in range(start, end + 1, step):
                payload = await self.instant_query(cluster, names[template], timestamp)
                for row in payload["data"]["result"]:
                    labels = row["metric"]
                    key = json.dumps(labels, sort_keys=True)
                    if key not in rows:
                        rows[key] = {"metric": labels, "values": []}
                    rows[key]["values"].append([timestamp, row["value"][1]])
            return {"status": "success", "data": {"resultType": "matrix", "result": list(rows.values())}}
        if self.settings.mock_scenario == "partial" and cluster == "zw":
            raise SourceError("演示：历史查询超时")
        if template == "source_age":
            return {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": []
                    if self.settings.mock_scenario == "empty"
                    else [
                        {
                            "metric": {},
                            "values": [
                                [ts, str(600 if self.settings.mock_scenario == "stale" else 2)]
                                for ts in range(start, end + 1, step)
                            ],
                        }
                    ],
                },
            }
        gpus = [g for g in self.fixture[cluster] if device is None or (g["node"], g["uuid"]) == device]
        if self.settings.mock_scenario == "empty":
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}
        capacity = sum(g["capacity"] for g in gpus)
        allocated = sum(a["value"] for g in gpus for a in g["allocations"])
        values = []
        for index, timestamp in enumerate(range(start, end + 1, step)):
            value = (
                capacity
                if template == "capacity"
                else round(allocated * (0.9 + 0.1 * math.sin(timestamp / 7200)), 2)
            )
            if self.settings.mock_scenario == "overallocated" and template == "allocated":
                value = capacity + 2000
            if self.settings.mock_scenario == "missing" and template == "allocated" and index % 7 == 3:
                continue
            values.append([timestamp, str(value)])
        return {
            "status": "success",
            "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}]},
        }
