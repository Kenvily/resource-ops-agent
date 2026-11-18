# ruff: noqa: F811
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from test_efficiency import collect_hour
from test_insights import Offline, complete_history, history  # noqa: F401

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.efficiency.agent import EfficiencyAgent
from app.efficiency.service import EfficiencyRequest, EfficiencyService
from app.insights.models import DiagnosisRequest
from app.insights.service import InsightService
from app.investigations.routes import FeedbackRequest
from app.investigations.service import InvestigationService
from app.main import create_app


@pytest_asyncio.fixture
async def tasks(history):
    h, source, config, now, _ = history
    config.investigation_feedback_enabled = True
    async with h.store.pool.connection() as c:
        await c.execute("TRUNCATE agent_task_memory,investigation_feedback,investigation_event,investigation_task")
    return InvestigationService(h, config), h, source, config, now


async def discard(_):
    pass


async def test_change_workflow_has_durable_id_inputs_evidence_and_idempotent_result(tasks, history):
    service, h, _source, config, now = tasks
    await complete_history(history, "allocation")
    agent = InsightService(h, config, Offline())
    query = DiagnosisRequest(cluster="yz", window="1h")
    events = []
    tid = uuid4()

    async def emit(e):
        events.append(e)

    result = await service.run(
        "allocation_change", query, emit, lambda emit: agent.run(query, emit, now), tid
    )
    assert events[0]["type"] == "task" and events[0]["data"]["id"] == str(tid)
    assert result["investigation"]["id"] == str(tid)
    detail = await service.detail(tid)
    assert detail["status"] in ("complete", "limited")
    assert detail["evidence"] and all("arguments" in e for e in detail["evidence"])
    assert detail["findings"][0]["evidence_ids"] and detail["findings"][0]["level"] == "fact"

    async def forbidden(_):
        raise AssertionError("idempotent retry executed tools")

    cached = await service.run("allocation_change", query, discard, forbidden, tid)
    assert cached["investigation"]["reused"]
    with pytest.raises(SourceError):
        await service.run(
            "allocation_change", DiagnosisRequest(cluster="zw", window="1h"), discard, forbidden, tid
        )


async def test_usage_workflow_persists_through_common_service_and_retains_old_report(tasks):
    service, h, source, config, now = tasks
    config.efficiency_enabled = True
    usage = EfficiencyService(h, config)
    now, _ = await collect_hour(usage)
    usage.agent = EfficiencyAgent(usage, Offline())

    async def forbidden(*args, **kwargs):
        raise AssertionError("investigation reached source")

    source.instant_query = forbidden
    source.range_query = forbidden
    query = EfficiencyRequest(cluster="yz")
    result = await service.run("gpu_usage", query, discard, lambda emit: usage.agent.run(query, emit, now))
    detail = await service.detail(UUID(result["investigation"]["id"]))
    assert detail["evidence"] and detail["kind"] == "gpu_usage"
    assert (await usage.reports.load(result["run_id"]))["agent"] == result["agent"]
    assert all(f["level"] in ("inference", "unknown") for f in detail["findings"])


async def test_cancellation_failure_and_expired_running_task_keep_partial_evidence(tasks):
    service, h, _, _, _ = tasks
    query = DiagnosisRequest(cluster="yz", window="1h")
    entered = asyncio.Event()
    tid = uuid4()

    async def blocked(emit):
        await emit(
            {
                "type": "evidence",
                "data": {"id": "e1", "tool": "quality", "label": "覆盖核验", "data": {"complete": False}},
            }
        )
        entered.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(service.run("allocation_change", query, discard, blocked, tid))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    detail = await service.detail(tid)
    assert detail["status"] == "cancelled" and len(detail["evidence"]) == 1 and not detail["findings"]

    async def fail(_):
        raise RuntimeError("PRIVATE-EXCEPTION")

    bad = uuid4()
    with pytest.raises(RuntimeError):
        await service.run("allocation_change", query, discard, fail, bad)
    detail = await service.detail(bad)
    assert detail["status"] == "failed" and "PRIVATE" not in detail["failure_reason"]
    lost = uuid4()
    await service.begin(lost, "allocation_change", ["yz"], query.model_dump(mode="json"))
    async with h.store.pool.connection() as c:
        await c.execute(
            "UPDATE investigation_task SET deadline=now()-interval '1 second' WHERE id=%s", (lost,)
        )
    assert (await service.detail(lost))["status"] == "interrupted"


async def test_feedback_scope_and_append_idempotency(tasks):
    service, _h, _, config, _ = tasks
    tid = uuid4()
    query = DiagnosisRequest(cluster="yz")

    async def work(_):
        return {"status": "limited", "evidence": [], "findings": []}

    await service.run("allocation_change", query, discard, work, tid)
    feedback = FeedbackRequest(request_id=uuid4(), decision="insufficient", note="需补充覆盖")
    await service.feedback(tid, feedback)
    await service.feedback(tid, feedback)
    assert len((await service.detail(tid))["feedback"]) == 1
    with pytest.raises(SourceError):
        await service.feedback(tid, feedback.model_copy(update={"note": "different"}))
    config.investigation_feedback_enabled = False
    with pytest.raises(SourceError):
        await service.feedback(tid, FeedbackRequest(request_id=uuid4(), decision="accepted"))
    config.insight_allowed_clusters = ["zw"]
    with pytest.raises(SourceError):
        await service.detail(tid)


async def test_http_stream_history_and_feedback_guards(tasks):
    _service, _h, _source, config, _ = tasks
    app = create_app(config, MockPrometheusClient(config), Offline())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        tid = str(uuid4())
        body = {"cluster": "yz", "window": "1h"}
        response = await client.post("/api/v1/insights/diagnose", json=body, headers={"Idempotency-Key": tid})
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["type"] == "task" and events[-1]["type"] == "result"
        report = await client.get("/api/v1/investigations/" + tid)
        assert report.status_code == 200 and report.json()["id"] == tid
        assert (await client.get("/api/v1/investigations/" + tid + "?cluster=zw")).status_code == 404
        feedback = {"request_id": str(uuid4()), "decision": "follow_up", "note": "核查"}
        assert (
            await client.post(
                "/api/v1/investigations/" + tid + "/feedback",
                json=feedback,
                headers={"origin": "https://untrusted.example"},
            )
        ).status_code == 403
        assert (
            await client.post("/api/v1/investigations/" + tid + "/feedback", json=feedback)
        ).status_code == 200
        assert (await client.get("/api/v1/investigations?cluster=yz")).json()["tasks"]


async def test_schema_eight_upgrade_imports_old_reports_without_rewriting_evidence(tasks):
    from psycopg.types.json import Jsonb

    service, h, _, _, _ = tasks
    legacy = uuid4()
    raw = {
        "request": {"hours": 1},
        "clusters": [{"cluster": "yz"}],
        "agent": {"status": "limited", "evidence": []},
    }
    async with h.store.pool.connection() as c:
        await c.execute(
            "INSERT INTO efficiency_investigation(id,clusters,request,result) VALUES(%s,%s,%s,%s)",
            (legacy, ["yz"], Jsonb(raw["request"]), Jsonb(raw)),
        )
        await c.execute(
            "DROP TABLE agent_task_memory,workload_resource_snapshot,host_metric_bucket,host_history_coverage,operations_decision,operations_record,operations_issue_event,operations_issue,investigation_feedback,investigation_event,investigation_task,node_metric_snapshot"
        )
        await c.execute("UPDATE history_metadata SET version=8 WHERE id=1")
    await h.store.initialize()
    await h.store.initialize()
    detail = await service.detail(legacy)
    assert detail["legacy"] and detail["result"] == raw and not detail["events"]
