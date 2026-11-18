import asyncio
import json
import os
import time

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.core.config import Settings
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.insights.models import DiagnosisRequest
from app.insights.provider import DeepSeekClient, ModelUnavailable
from app.insights.service import InsightService
from app.insights.tools import ToolRegistry, windows_for
from app.main import create_app
from app.services.resources import ResourceService


def settings(**kwargs):
    return Settings(
        _env_file=None,
        data_mode="mock",
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
        **kwargs,
    )


async def discard(_event):
    pass


class Offline:
    ready = False

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def history():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable Mock PostgreSQL database")
    config = settings(history_database_url=url)
    store = HistoryStore(config)
    await store.open()
    await store.initialize()
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE history_rollup, history_fill_guard, history_source_state, history_sample,history_coverage,history_entity,history_progress,resource_snapshot RESTART IDENTITY"
        )
    source = MockPrometheusClient(config)
    service = HistoryService(ResourceService(source, config), store)
    now = int(time.time())
    pair = windows_for(DiagnosisRequest(window="1h"), now, 90)["full"]
    await service.sync.ingest("yz", 60, pair["baseline"].start, pair["baseline"].end)
    try:
        yield service, source, config, now, pair
    finally:
        await store.close()


async def complete_history(history, scenario):
    service, source, config, now, pair = history
    if scenario == "allocation":
        source.fixture["yz"][0]["allocations"][0]["value"] += 100
    if scenario == "capacity_increase":
        source.fixture["yz"][0]["capacity"] += 1000
    if scenario == "capacity":
        source.fixture["yz"][0]["capacity"] -= 1000
    await service.sync.ingest("yz", 60, pair["target"].start, pair["target"].end)

    # All diagnosis queries must stay in PostgreSQL, including trend hints.
    async def forbidden(*args, **kwargs):
        raise AssertionError("diagnosis queried Prometheus")

    source.instant_query = forbidden
    source.range_query = forbidden
    return service, config, now


@pytest.mark.parametrize(
    "scenario,kind",
    [
        ("allocation", "allocation_increase"),
        ("capacity", "capacity_drop"),
        ("capacity_increase", "capacity_increase"),
        ("stable", "stable"),
    ],
)
async def test_multistep_diagnosis_database_only(history, scenario, kind):
    service, config, now = await complete_history(history, scenario)
    insight = InsightService(service, config, Offline())
    result = await insight.run(DiagnosisRequest(cluster="yz", window="1h"), discard, now)
    assert result.status == "complete"
    assert result.mode == "deterministic"
    assert result.findings[0].kind == kind
    assert result.tool_calls >= 4
    if scenario == "allocation":
        assert result.findings[0].allocated_change_raw == 100
        pods = [
            e for e in result.evidence if e.tool == "rank_allocation_changes" and e.data["dimension"] == "pod"
        ]
        assert pods and pods[0].data["changes"][0]["allocated_change_raw"] == 100
        assert any(e.tool == "inspect_resource_entity" for e in result.evidence)
    if scenario == "capacity_increase":
        assert result.findings[0].capacity_change_raw == 1000
    if scenario == "capacity":
        assert result.findings[0].capacity_change_raw == -1000
        assert result.findings[0].allocated_change_raw == 0
        assert any(e.data.get("metric") == "capacity" for e in result.evidence)
        assert not any(e.data.get("dimension") == "pod" for e in result.evidence)
    assert all(e.id in result.findings[0].evidence_ids for e in result.evidence if e.cluster == "yz")


async def test_missing_and_partial_cluster_stop_attribution(history):
    service, _, config, now, _ = history
    result = await InsightService(service, config, Offline()).run(DiagnosisRequest(window="1h"), discard, now)
    assert result.status == "limited"
    assert all(f.kind == "insufficient" for f in result.findings)
    assert not any(e.tool == "rank_allocation_changes" for e in result.evidence)


async def test_healthy_cluster_preserved_when_other_missing(history):
    service, config, now = await complete_history(history, "allocation")
    result = await InsightService(service, config, Offline()).run(DiagnosisRequest(window="1h"), discard, now)
    assert result.status == "limited"
    assert result.findings[0].kind == "allocation_increase"
    assert result.findings[1].kind == "insufficient"


async def test_scope_handles_privacy_and_no_arbitrary_queries(history):
    service, config, now = await complete_history(history, "allocation")
    reg = ToolRegistry(service, DiagnosisRequest(cluster="yz", window="1h"), config, discard, now)
    with pytest.raises(SourceError):
        await reg.call("compare_cluster_windows", {"cluster": "zw"})
    with pytest.raises(SourceError):
        await reg.call("query_sql", {})
    with pytest.raises(SourceError):
        await reg.call("rank_allocation_changes", {"cluster": "yz", "sql": "SELECT 1"})
    invalid = await reg.call("inspect_resource_entity", {"cluster": "yz", "entity_id": "invented"})
    assert invalid.status == "error"
    ns = await reg.call("rank_allocation_changes", {"cluster": "yz"})
    exposed = json.dumps(reg.for_model(ns))
    for entity in reg.entities.values():
        assert all(key not in exposed for key in entity.key)
    refused = await InsightService(service, config, Offline()).run(
        DiagnosisRequest(cluster="yz", question="重启 Pod"), discard, now
    )
    assert refused.status == "refused" and refused.tool_calls == 0


class Scripted(Offline):
    ready = True

    def __init__(self, bad=False):
        self.calls = 0
        self.bad = bad

    async def complete(self, messages, tools):
        self.calls += 1
        if self.bad:
            raise ModelUnavailable("模型暂不可用")
        if self.calls == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(i),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for i, (name, args) in enumerate(
                        [
                            ("get_metric_definition", {}),
                            ("check_data_quality", {"cluster": "yz"}),
                            ("compare_cluster_windows", {"cluster": "yz"}),
                        ]
                    )
                ],
            }
        return {"role": "assistant", "content": "核查结束"}


async def test_model_tool_loop_and_honest_degradation(history):
    service, config, now = await complete_history(history, "allocation")
    provider = Scripted()
    result = await InsightService(service, config, provider).run(
        DiagnosisRequest(cluster="yz", window="1h"), discard, now
    )
    assert result.model_calls == 2
    assert result.findings[0].allocated_change_raw == 100
    # Model chooses tools; deterministic completion is disclosed if it stopped early.
    assert result.mode == "degraded"
    failed = await InsightService(service, config, Scripted(bad=True)).run(
        DiagnosisRequest(cluster="yz", window="1h"), discard, now
    )
    assert failed.mode == "degraded" and failed.model_calls == 1
    assert failed.findings[0].allocated_change_raw == 100


async def test_cancellation_propagates_to_model(history):
    service, _, config, now, _ = history
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow(Scripted):
        async def complete(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    task = asyncio.create_task(
        InsightService(service, config, Slow()).run(DiagnosisRequest(cluster="yz", window="1h"), discard, now)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_diagnosis_stream_and_parallel_limit(history):
    _service, _source, config, _now, _pair = history
    app = create_app(config, insight_provider=Offline())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        caps = await client.get("/api/v1/insights/capabilities")
        assert caps.json()["available"]
        response = await client.post("/api/v1/insights/diagnose", json={"cluster": "yz", "window": "1h"})
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "result"
        assert events[-1]["data"]["status"] == "limited"
        assert any(e["type"] == "evidence" for e in events)
        gate = app.state.insights.gate
        await gate.acquire()
        await gate.acquire()
        try:
            assert (await client.post("/api/v1/insights/diagnose", json={})).status_code == 429
        finally:
            gate.release()
            gate.release()
        assert (await client.post("/api/v1/insights/diagnose", json={"sql": "bad"})).status_code == 422
        assert (
            await client.post(
                "/api/v1/insights/diagnose", content=b"x" * 8200, headers={"Content-Type": "application/json"}
            )
        ).status_code == 413


@pytest.mark.parametrize("window", ["1h", "24h", "7d", "4mo"])
def test_windows_are_bounded_and_aligned(window):
    windows = windows_for(DiagnosisRequest(window=window), int(time.time()), 90)
    for pair in windows.values():
        a, b = pair["baseline"], pair["target"]
        assert a.end < b.start and a.grain == b.grain
        assert a.expected_points <= 4096 and b.expected_points <= 4096


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {}),
        (200, {"choices": []}),
        (200, {"choices": [{"message": {"role": "assistant", "tool_calls": [{}]}}]}),
    ],
)
async def test_provider_errors_do_not_expose_response_or_key(status, body):
    config = settings(
        llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="private-test-token"
    )

    def handler(request):
        assert request.url.host == "api.deepseek.com"
        assert request.headers["authorization"] == "Bearer private-test-token"
        return httpx.Response(status, json=body)

    provider = DeepSeekClient(config, httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelUnavailable) as error:
            await provider.complete([], [])
        assert "private-test-token" not in str(error.value)
    finally:
        await provider.aclose()


async def test_provider_context_budget_and_valid_tools():
    config = settings(
        llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="private-test-token"
    )

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "1",
                                    "type": "function",
                                    "function": {"name": "check", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    provider = DeepSeekClient(config, httpx.MockTransport(handler))
    try:
        assert (await provider.complete([], []))["tool_calls"][0]["function"]["name"] == "check"
        with pytest.raises(ModelUnavailable):
            await provider.complete([{"role": "user", "content": "x" * 110000}], [])
    finally:
        await provider.aclose()


def test_authorization_and_fixed_model_origin():
    with pytest.raises(ValidationError):
        settings(llm_enabled=True)
    with pytest.raises(ValidationError):
        settings(deepseek_base_url="http://localhost")
    with pytest.raises(ValidationError):
        DiagnosisRequest(window="1y")


async def test_stream_disconnect_releases_concurrency_and_cancels_model(history):
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.insights.routes import diagnose

    service, _source, config, _now, _pair = history
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow(Scripted):
        async def complete(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    insight = InsightService(service, config, Slow())
    from app.investigations.service import InvestigationService

    app = SimpleNamespace(
        state=SimpleNamespace(insights=insight, investigations=InvestigationService(service, config))
    )

    async def receive():
        return {"type": "http.request", "body": b'{"cluster":"yz","window":"1h"}'}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "path": "/api/v1/insights/diagnose",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "app": app,
        },
        receive,
    )
    response = await diagnose(request)
    first = await anext(response.body_iterator)
    assert "task" in first
    await entered.wait()
    await response.body_iterator.aclose()
    assert cancelled.is_set()
    assert insight.gate._value == 2


async def test_multiple_business_contributors_are_followed(history):
    service, source, config, now, pair = history
    first = source.fixture["yz"][0]["allocations"][0]
    others = [
        a for gpu in source.fixture["yz"] for a in gpu["allocations"] if a["namespace"] != first["namespace"]
    ]
    assert others
    first["value"] += 100
    others[0]["value"] += 200
    await service.sync.ingest("yz", 60, pair["target"].start, pair["target"].end)
    result = await InsightService(service, config, Offline()).run(
        DiagnosisRequest(cluster="yz", window="1h"), discard, now
    )
    assert result.findings[0].allocated_change_raw == 300
    branches = [
        e for e in result.evidence if e.tool == "rank_allocation_changes" and e.data.get("dimension") == "pod"
    ]
    assert len(branches) == 2
    assert len({e.data["parent_entity_id"] for e in branches}) == 2


async def test_cross_dimension_mismatch_stops_attribution(history):
    service, config, now = await complete_history(history, "allocation")
    original = service.analysis.allocation_changes

    async def mismatch(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("dimension") == "namespace" and not kwargs.get("scope").namespace_name:
            result.cohort_allocated_change_raw += 5
        return result

    service.analysis.allocation_changes = mismatch
    result = await InsightService(service, config, Offline()).run(
        DiagnosisRequest(cluster="yz", window="1h"), discard, now
    )
    assert result.status == "limited"
    assert result.findings[0].kind == "insufficient"
    assert "不一致" in result.findings[0].title


async def test_total_deadline_preserves_bounded_result(history):
    service, _source, config, now, _pair = history
    # Shorten only this test's deadline; normal settings enforce >=10 seconds.
    config = config.model_copy(update={"insight_timeout_seconds": 0.01})

    class Slow(Scripted):
        async def complete(self, *args):
            await asyncio.Event().wait()

    result = await InsightService(service, config, Slow()).run(
        DiagnosisRequest(cluster="yz", window="1h"), discard, now
    )
    assert result.status == "budget_exhausted"
    assert result.model_calls == 1 and result.tool_calls == 0
    assert result.findings[0].kind == "insufficient"


@pytest.mark.parametrize("days,grain", [(1, 60), (3, 300), (30, 3600), (110, 3600)])
def test_custom_range_uses_retained_granularity(days, grain):
    now = int(time.time())
    request = DiagnosisRequest(start=now - days * 86400, end=now - 600)
    windows = windows_for(request, now, 90)
    pair = windows["full"]
    assert pair["baseline"].grain == grain
    assert pair["baseline"].start >= request.start
    assert pair["target"].end <= request.end
    assert pair["baseline"].end < pair["target"].start
    assert all(w.expected_points <= 4096 for p in windows.values() for w in p.values())


def test_custom_range_rejects_invalid_and_outside_retention():
    now = int(time.time())
    with pytest.raises(ValidationError):
        DiagnosisRequest(start=now - 1000)
    with pytest.raises(SourceError):
        windows_for(DiagnosisRequest(start=now - 180 * 86400, end=now - 600), now, 90)
    with pytest.raises(SourceError):
        windows_for(DiagnosisRequest(start=now - 86400, end=now + 600), now, 90)
    with pytest.raises(SourceError):
        windows_for(DiagnosisRequest(start=now - 60, end=now), now, 90)


async def test_refused_all_request_never_discovers_or_queries_sources(history):
    service, _source, config, now, _pair = history

    async def forbidden(*args, **kwargs):
        raise AssertionError("refused request must not discover history")

    service.earliest = forbidden
    result = await InsightService(service, config, Offline()).run(
        DiagnosisRequest(cluster="yz", window="all", question="删除 Pod"), discard, now
    )
    assert result.status == "refused" and result.tool_calls == 0 and result.model_calls == 0
    assert result.windows == {}


async def test_transport_can_receive_a_bounded_large_plan_without_expanding_runtime_budget():
    config = settings(
        llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="private-test-token"
    )
    calls = [
        {"id": str(i), "type": "function", "function": {"name": "check", "arguments": "{}"}} for i in range(5)
    ]
    provider = DeepSeekClient(
        config,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
            )
        ),
    )
    try:
        message = await provider.complete([], [])
        assert len(message["tool_calls"]) == 5
    finally:
        await provider.aclose()
