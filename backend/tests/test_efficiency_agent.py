# ruff: noqa: F811
# Imported pytest fixtures are intentionally shadowed by test parameters.
import asyncio
import json

import httpx
import pytest
from test_efficiency import (
    collect_hour,
    settings,
    stored,  # noqa: F401
)

from app.clients.prometheus import SourceError
from app.efficiency.agent import EfficiencyAgent, UsageRegistry
from app.efficiency.service import EfficiencyRequest
from app.insights.provider import ModelUnavailable
from app.main import create_app


class Offline:
    ready = False


class Planner:
    ready = True

    def __init__(self):
        self.calls = 0
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(json.dumps(messages, ensure_ascii=False))
        self.calls += 1

        def call(name, args, index):
            return {
                "id": f"call-{self.calls}-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }

        if self.calls == 1:
            calls = [
                call("check_usage_coverage", {"cluster": "yz"}, 0),
                call("find_low_usage_candidates", {"cluster": "yz"}, 1),
            ]
        elif self.calls in (2, 3):
            candidates = next(
                json.loads(m["content"])["data"]["entities"]
                for m in messages
                if m["role"] == "tool" and '"entities"' in m["content"]
            )
            candidates = [row for row in candidates if row["classification"] == "review_candidate"]
            target = candidates[self.calls - 2]["entity_id"]
            calls = [
                call(name, {"entity_id": target}, i)
                for i, name in enumerate(
                    (
                        "inspect_gpu_usage",
                        "trace_workload_lifecycle",
                        "check_container_signals",
                    )
                )
            ]
        else:
            calls = []
        return {"role": "assistant", "content": None, "tool_calls": calls}


async def discard(_event):
    pass


async def prepared(stored, provider=None):
    _config, db, source, service = stored
    now, _ = await collect_hour(service)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Agent queried Prometheus")

    source.range_query = forbidden
    source.instant_query = forbidden
    service.agent = EfficiencyAgent(service, provider or Offline())
    return service, now, db


async def test_multistep_evidence_is_persisted_and_private(stored):
    provider = Planner()
    service, now, _db = await prepared(stored, provider)
    result = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    assert result["agent"]["model_calls"] == 4
    assert result["agent"]["mode"] == "deepseek"
    assert {e["tool"] for e in result["agent"]["evidence"]} >= {
        "check_usage_coverage",
        "find_low_usage_candidates",
        "trace_workload_lifecycle",
        "check_container_signals",
    }
    assert result["run_id"] and all(r["case_id"] for r in result["results"])
    saved = await service.reports.load(result["run_id"])
    assert saved == result
    external = "".join(provider.messages)
    assert "GPU-DEMO" not in external and "model-serving" not in external and "UID-" not in external
    assert "MOCK-NVIDIA" not in external
    assert result["agent"]["findings"]
    signals = [e for e in result["agent"]["evidence"] if e["tool"] == "check_container_signals"]
    assert signals[0]["data"]["task_utilization_available"] is False


async def test_model_failure_is_explicit_and_deterministic_evidence_survives(stored):
    class Broken:
        ready = True

        async def complete(self, *_):
            raise ModelUnavailable("offline")

    service, now, _ = await prepared(stored, Broken())
    result = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    assert result["agent"]["mode"] == "degraded"
    assert result["agent"]["warnings"]
    assert result["agent"]["tool_calls"] >= 6
    assert all(i["investigation"]["complete"] for i in result["results"] if "investigation" in i)


async def test_opaque_tools_reject_unreturned_or_cross_cluster_entities(stored):
    service, now, _ = await prepared(stored)
    bundles = {}
    request = EfficiencyRequest(cluster="yz")
    report = await service.evaluate(request, now, bundles=bundles)
    registry = UsageRegistry(service, request, report, bundles, {"yz": bundles["yz"][3]}, discard)
    with pytest.raises(SourceError):
        await registry.call("inspect_gpu_usage", {"entity_id": "gpu-1"})
    with pytest.raises(SourceError):
        await registry.call("find_low_usage_candidates", {"cluster": "zw"})
    with pytest.raises(SourceError):
        await registry.call("http", {"url": "https://bad"})
    await registry.call("find_low_usage_candidates", {"cluster": "yz"})
    first = await registry.call("inspect_gpu_usage", {"entity_id": "gpu-1"})
    assert await registry.call("inspect_gpu_usage", {"entity_id": "gpu-1", "segment": "full"}) == first
    with pytest.raises(SourceError):
        await registry.call("inspect_gpu_usage", {"entity_id": "gpu-1", "segment": "http://bad"})


async def test_saved_reports_remain_immutable_and_scoped(stored):
    service, now, _ = await prepared(stored)
    report = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    saved = await service.reports.load(report["run_id"])
    report["results"][0]["mean_utilization_pct"] = 99
    assert await service.reports.load(report["run_id"]) == saved
    service.settings.insight_allowed_clusters = ["zw"]
    with pytest.raises(SourceError):
        await service.reports.load(report["run_id"])


async def test_cancel_stops_model_and_does_not_save_a_completed_report(stored):
    class Waiting:
        ready = True

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = False

        async def complete(self, *_):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    provider = Waiting()
    service, now, db = await prepared(stored, provider)
    task = asyncio.create_task(service.agent.run(EfficiencyRequest(cluster="yz"), discard, now))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled
    async with db.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM efficiency_investigation")).fetchone())[
            0
        ] == 0


async def test_stream_report_roundtrip_and_removed_review_endpoints(stored):
    service, _, _ = await prepared(stored)
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        app.state.efficiency = service
        from app.investigations.service import InvestigationService

        app.state.investigations = InvestigationService(service.history, service.settings)
        response = await client.post("/api/v1/efficiency/diagnose", json={"cluster": "yz"})
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        result = next(event["data"] for event in events if event["type"] == "result")
        assert result["agent"]["mode"] == "deterministic"
        assert (await client.get(f"/api/v1/efficiency/reports/{result['run_id']}")).status_code == 200
        assert (await client.post("/api/v1/efficiency/reviews", json={})).status_code == 405
        assert (await client.get("/api/v1/efficiency/evaluation")).status_code == 404
        assert (await client.get(f"/api/v1/efficiency/reports/{result['run_id']}/reviews")).status_code == 404
        assert (
            await client.post(
                "/api/v1/efficiency/diagnose", json={"cluster": "zw"}, headers={"Origin": "https://bad"}
            )
        ).status_code == 403


async def test_verified_container_signals_and_conflicting_zero(stored):
    config, _, source, service = stored
    config.efficiency_verified_device_models = ["MOCK-NVIDIA"]
    now, _ = await collect_hour(service)
    service.agent = EfficiencyAgent(service, Offline())
    report = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    signals = [e["data"] for e in report["agent"]["evidence"] if e["tool"] == "check_container_signals"]
    assert signals and signals[0]["task_utilization_available"] is True
    original = source.range_query

    async def zero_util(*args, **kwargs):
        payload = await original(*args, **kwargs)
        if args[1] in ("eff_container_utilization", "eff_kernel_age"):
            for row in payload["data"]["result"]:
                for point in row["values"]:
                    point[1] = "0" if args[1] == "eff_container_utilization" else "1"
        return payload

    source.range_query = zero_util
    await collect_hour(service, now=now)
    report = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    signals = [e["data"] for e in report["agent"]["evidence"] if e["tool"] == "check_container_signals"]
    assert signals[0]["task_utilization_available"] is False
    assert signals[0]["signals"][0]["zero_conflicts_with_activity"] is True


async def test_optional_source_failure_keeps_host_evidence_and_marks_unknown(stored):
    _, _, source, service = stored
    original = source.range_query

    async def no_container(*args, **kwargs):
        if args[1] in ("eff_container_utilization", "eff_container_memory", "eff_kernel_age"):
            raise SourceError("unavailable")
        return await original(*args, **kwargs)

    source.range_query = no_container
    now, _ = await collect_hour(service)
    service.agent = EfficiencyAgent(service, Offline())
    report = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    assert report["clusters"][0]["candidate_count"] > 0
    signal = next(e["data"] for e in report["agent"]["evidence"] if e["tool"] == "check_container_signals")
    assert signal["task_utilization_available"] is False
    assert all(r["memory_peak_bytes"] is None for r in signal["signals"])


async def test_idle_task_on_busy_shared_gpu_is_found_without_host_apportionment(stored):
    config, _, source, service = stored
    config.efficiency_verified_device_models = ["MOCK-NVIDIA"]
    gpu = source.fixture["yz"][0]
    idle = gpu["allocations"][0]["pod"]
    gpu["allocations"].append({"namespace": "model-serving", "pod": "active-peer", "value": 4000})
    original = source.range_query

    async def shared(*args, **kwargs):
        payload = await original(*args, **kwargs)
        name = args[1]
        for row in payload["data"]["result"]:
            labels = row["metric"]
            if labels.get("device_uuid") != gpu["uuid"]:
                continue
            if name == "eff_utilization":
                value = 80
            elif name == "eff_memory":
                value = 8e9
            elif name == "eff_container_utilization":
                value = 0 if labels.get("exported_pod") == idle else 70
            elif name == "eff_container_memory":
                value = 128e6 if labels.get("exported_pod") == idle else 8e9
            elif name == "eff_kernel_age":
                value = 7200 if labels.get("exported_pod") == idle else 1
            else:
                continue
            for point in row["values"]:
                point[1] = str(value)
        return payload

    source.range_query = shared
    now, _ = await collect_hour(service)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Agent must read database only")

    source.range_query = forbidden
    service.agent = EfficiencyAgent(service, Offline())
    report = await service.agent.run(EfficiencyRequest(cluster="yz"), discard, now)
    target = next(r for r in report["results"] if r["device_uuid"] == gpu["uuid"])
    assert target["classification"] == "active_compute"
    assert len(target["task_candidates"]) == 1
    assert target["task_candidates"][0]["pod"] == idle
    assert target["task_candidates"][0]["mean_utilization_pct"] == 0
    assert target["investigation"]["analysis"]["outcome"] == "verified_task_candidate"


def test_task_entity_budget_is_bounded_before_expanding_time_grids():
    from app.efficiency.tasks import task_candidates

    rows = [
        (1800000000, "node", "gpu", "ns", f"pod-{i}", "worker", f"uid-{i}", 10000, 1700000000, True, 0, True)
        for i in range(1001)
    ]
    with pytest.raises(SourceError, match="1000"):
        task_candidates(EfficiencyRequest(), ([], [], rows, []), [], 1800000000 - 86400, 1800000000)
