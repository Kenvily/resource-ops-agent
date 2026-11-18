# ruff: noqa: F811
"""Mock PostgreSQL: immutable deliveries, guarded writes, concurrency and evidence retention."""

import asyncio
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from test_insights import history  # noqa: F401

from app.history.snapshots import SnapshotService
from app.insights.assessment import AssessmentRequest, AssessmentService
from app.investigations.service import InvestigationService
from app.main import create_app
from app.operations.metrics import MetricsService
from app.operations.reports import ReportRequest, ReportService
from app.operations.workspace import WorkspaceError, WorkspaceService
from app.operations.workspace_routes import CaptureIssue, Decision, IssueUpdate, SaveAssessment, SaveReport


@pytest_asyncio.fixture
async def workspace(history):
    h, _, settings, _, _ = history
    settings.operations_workspace_enabled = True
    async with h.store.pool.connection() as c:
        await c.execute(
            "TRUNCATE operations_decision,operations_record,operations_issue_event,operations_issue"
        )
    snapshots = SnapshotService(h.resources, h.store)
    await snapshots.update_all()
    metrics = MetricsService(h, settings)
    tasks = InvestigationService(h, settings)
    assessment = AssessmentService(snapshots, h, settings)
    service = WorkspaceService(
        h, settings, assessment, ReportService(snapshots, h, metrics, settings), metrics, tasks
    )
    return service, h, settings


async def test_assessment_freezes_db_only_result_and_deduplicates_concurrent_uuid(workspace):
    service, h, _ = workspace

    async def forbidden(*a, **kw):
        raise AssertionError("planning read source instead of DB")

    h.resources.client.instant_query = forbidden
    query = SaveAssessment(request_id=uuid4(), requirement=AssessmentRequest(cluster="yz", gpu_count=1))
    a, b = await asyncio.gather(
        service.create_record("assessment", query), service.create_record("assessment", query)
    )
    assert a["result"] == b["result"] and a["id"] == b["id"]
    assert a["result"]["candidates"]
    query.requirement.gpu_count = 2
    with pytest.raises(WorkspaceError, match="其他内容"):
        await service.create_record("assessment", query)
    assert len((await service.recent("assessment", "yz"))["records"]) == 1
    with pytest.raises(WorkspaceError):
        await service.detail(a["id"], "zw")


async def test_review_selection_is_validated_and_append_only(workspace):
    service, _, _ = workspace
    record = await service.create_record(
        "assessment",
        SaveAssessment(request_id=uuid4(), requirement=AssessmentRequest(cluster="yz", gpu_count=1)),
    )
    with pytest.raises(WorkspaceError):
        await service.review(record["id"], Decision(request_id=uuid4(), decision="accepted"))
    with pytest.raises(WorkspaceError):
        await service.review(
            record["id"], Decision(request_id=uuid4(), decision="accepted", candidates=[999])
        )
    query = Decision(request_id=uuid4(), decision="accepted", candidates=[0], note="候选额度需平台确认")
    await service.review(record["id"], query)
    await service.review(record["id"], query)
    query.note = "改变原复核"
    with pytest.raises(WorkspaceError):
        await service.review(record["id"], query)
    loaded = await service.detail(record["id"])
    assert len(loaded["decisions"]) == 1 and loaded["result"] == record["result"]


async def metric_alerts(service):
    async def read(_):
        return [
            {
                "cluster": "yz",
                "key": "gpu_temp",
                "status": "ready",
                "evaluated_at": 100,
                "values": [{"node": "mock-node", "device_uuid": "mock-gpu", "value": 95}],
            }
        ]

    service.metrics.read = read
    return CaptureIssue(
        request_id=uuid4(), cluster="yz", metric="gpu_temp", node="mock-node", device_uuid="mock-gpu"
    )


async def seed_task(service, h, cluster="yz"):
    task_id = uuid4()
    async with h.store.pool.connection() as c:
        await c.execute(
            "INSERT INTO investigation_task(id,kind,status,clusters,request,result,deadline,finished_at) VALUES(%s,'allocation_change','limited',%s,%s,%s,now(),now())",
            (
                task_id,
                [cluster],
                Jsonb({"cluster": cluster, "window": "1h"}),
                Jsonb(
                    {
                        "evidence": [{"id": "e1", "tool": "check_data_quality", "data": {"coverage": 0.5}}],
                        "findings": [],
                        "warnings": ["历史缺测"],
                    }
                ),
            ),
        )
    return task_id


async def test_issue_capture_dedup_revision_conflict_and_closed_recurrence(workspace):
    service, h, _ = workspace
    query = await metric_alerts(service)
    a, b = await asyncio.gather(service.capture_issue(query), service.capture_issue(query))
    assert a["id"] == b["id"]
    task_id = await seed_task(service, h)
    update = IssueUpdate(request_id=uuid4(), revision=1, status="investigating", investigation_id=task_id)
    await service.transition(a["id"], update)
    await service.transition(a["id"], update)
    with pytest.raises(WorkspaceError, match="已被更新"):
        await service.transition(
            a["id"], IssueUpdate(request_id=uuid4(), revision=1, status="resolved", note="核查结束")
        )
    await service.transition(
        a["id"], IssueUpdate(request_id=uuid4(), revision=2, status="resolved", note="已在平台核实")
    )
    async with h.store.pool.connection() as c:
        await c.execute("DELETE FROM investigation_task WHERE id=%s", (task_id,))
    await service.transition(a["id"], update)  # retry uses frozen link, never reopens source
    detail = await service.issue_detail(a["id"])
    assert detail["revision"] == 3 and len(detail["events"]) == 2
    query.request_id = uuid4()
    assert (await service.capture_issue(query))["id"] != a["id"]
    with pytest.raises(WorkspaceError):
        await service.issue_detail(a["id"], "zw")
    with pytest.raises(WorkspaceError, match="已有同类"):
        await service.transition(a["id"], IssueUpdate(request_id=uuid4(), revision=3, status="open"))
    query.node = "client-invented-node"
    with pytest.raises(WorkspaceError):
        await service.capture_issue(query)


async def test_report_freezes_linked_evidence_and_preserves_metrics_after_source_purge(workspace):
    service, h, _ = workspace
    query = await metric_alerts(service)
    issue = await service.capture_issue(query)
    task_id = await seed_task(service, h)
    request = SaveReport(
        request_id=uuid4(),
        report=ReportRequest(cluster="yz"),
        investigation_ids=[task_id],
        issue_ids=[issue["id"]],
    )
    report = await service.create_record("report", request)
    result = report["result"]
    assert result["linked_investigations"][0]["evidence"] and result["trace_warnings"]
    await service.transition(
        issue["id"], IssueUpdate(request_id=uuid4(), revision=1, status="resolved", note="结束核查")
    )
    async with h.store.pool.connection() as c:
        await c.execute("DELETE FROM investigation_task WHERE id=%s", (task_id,))
    loaded = await service.detail(report["id"])
    assert loaded["result"] == result
    assert loaded["result"]["linked_issues"][0]["status"] == "open"
    assert (await service.create_record("report", request))["result"] == result


async def test_links_reject_cross_cluster_and_nonterminal_tasks(workspace):
    service, h, _ = workspace
    tid = await seed_task(service, h, "zw")
    with pytest.raises(WorkspaceError):
        await service.create_record(
            "report",
            SaveReport(request_id=uuid4(), report=ReportRequest(cluster="yz"), investigation_ids=[tid]),
        )
    async with h.store.pool.connection() as c:
        await c.execute("UPDATE investigation_task SET status='running' WHERE id=%s", (tid,))
    with pytest.raises(WorkspaceError):
        await service.freeze_investigation(tid, ["zw"])


async def test_workspace_http_controls_content_origin_proxy_scope_and_retry(workspace):
    service, _, config = workspace
    app = create_app(config)
    # Mock lifespan without DB, then inject tested database service.
    config.history_database_url = SecretStr("")
    config.host_metrics_enabled = False
    config.efficiency_enabled = False
    async with app.router.lifespan_context(app):
        app.state.workspace = service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
        ) as client:
            assert (
                await client.get("/api/v1/operations/workspace/records?kind=assessment&cluster=yz")
            ).status_code == 200
            query = {"request_id": str(uuid4()), "requirement": {"cluster": "yz", "gpu_count": 1}}
            assert (
                await client.post(
                    "/api/v1/operations/workspace/assessments",
                    json=query,
                    headers={"Origin": "https://other.example"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/v1/operations/workspace/assessments",
                    json=query,
                    headers={"X-Forwarded-For": "127.0.0.1"},
                )
            ).status_code == 403
            assert (
                await client.post("/api/v1/operations/workspace/assessments", content="{}")
            ).status_code == 415
            response = await client.post("/api/v1/operations/workspace/assessments", json=query)
            assert response.status_code == 200
            rid = response.json()["id"]
            assert (
                await client.get(f"/api/v1/operations/workspace/records/{rid}?cluster=zw")
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/operations/workspace/records/{rid}/review",
                    json={"request_id": str(uuid4()), "decision": "accepted", "candidates": [-1]},
                )
            ).status_code == 422
            config.operations_workspace_enabled = False
            assert (
                await client.post("/api/v1/operations/workspace/assessments", json=query)
            ).status_code == 403


async def test_schema_nine_upgrade_is_repeatable_and_preserves_saved_investigation(workspace):
    service, h, _ = workspace
    tid = await seed_task(service, h)
    async with h.store.pool.connection() as c:
        await c.execute(
            "DROP TABLE agent_task_memory,workload_resource_snapshot,host_metric_bucket,host_history_coverage,operations_decision,operations_record,operations_issue_event,operations_issue"
        )
        await c.execute("UPDATE history_metadata SET version=9 WHERE id=1")
    await h.store.initialize()
    await h.store.initialize()
    assert (await service.investigations.detail(tid))["status"] == "limited"


async def test_single_cluster_authorization_can_review_own_records_and_issues(workspace):
    service, _, settings = workspace
    record = await service.create_record(
        "assessment",
        SaveAssessment(request_id=uuid4(), requirement=AssessmentRequest(cluster="yz", gpu_count=1)),
    )
    issue = await service.capture_issue(await metric_alerts(service))
    settings.insight_allowed_clusters = ["yz"]
    await service.review(record["id"], Decision(request_id=uuid4(), decision="accepted", candidates=[0]))
    await service.transition(
        issue["id"], IssueUpdate(request_id=uuid4(), revision=1, status="resolved", note="单集群核查完成")
    )
    assert (await service.detail(record["id"], "yz"))["decisions"]
    with pytest.raises(WorkspaceError):
        await service.issues("zw")
