"""Operational reports, fixed metric semantics and evidence-based infrastructure decisions."""

# ruff: noqa: F811
import time

import httpx
import pytest
from pydantic import SecretStr
from test_analysis import context  # noqa: F401

from app.clients.prometheus import SourceError
from app.core.telemetry import Telemetry
from app.history.snapshots import SnapshotService
from app.main import create_app
from app.operations.catalog import CATALOG, query
from app.operations.infrastructure import assessment, infrastructure
from app.operations.metrics import MetricsService, alerts, normalize
from app.operations.reports import ReportRequest, ReportService, windows


def payload(rows, at):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"cluster": "k8s_yz_gpu_maas", **labels}, "values": [[at, str(value)]]}
                for labels, value in rows
            ],
        },
    }


@pytest.mark.parametrize("window,grain,points", [("1d", 300, 288), ("1w", 3600, 168), ("1mo", 3600, 720)])
def test_report_windows_do_not_overlap_or_overrun_retention(window, grain, points):
    a, b = windows(window, int(time.time()))
    assert a.grain == b.grain == grain
    assert a.expected_points == b.expected_points == points
    assert a.end + grain == b.start
    assert b.end <= time.time() - 120


def test_metrics_deduplicate_conflicts_and_do_not_sum_replicas():
    at = 1793237400
    rows = [({"UUID": "GPU-MOCK"}, 40), ({"UUID": "GPU-MOCK"}, 40)]
    result = normalize("gpu_temp", payload(rows, at), "yz", at, {("node", "GPU-MOCK")}, [])
    assert result["status"] == "ready" and result["values"][0]["value"] == 40
    rows.append(({"UUID": "GPU-MOCK"}, 99))
    result = normalize("gpu_temp", payload(rows, at), "yz", at, {("node", "GPU-MOCK")}, [])
    assert result["values"] == [] and result["rejected_series"] > 0


@pytest.mark.parametrize(
    "labels,value",
    [
        ({"UUID": "wrong"}, 20),
        ({"UUID": "GPU-MOCK", "device_uuid": "conflict"}, 20),
        ({"UUID": "GPU-MOCK", "GPU_I_ID": "1"}, 20),
        ({"UUID": "GPU-MOCK"}, 9223372036854775807),
    ],
)
def test_unknown_identity_mig_and_sentinel_do_not_become_valid_metric(labels, value):
    result = normalize("gpu_temp", payload([(labels, value)], 60), "yz", 60, {("node", "GPU-MOCK")}, [])
    assert not result["values"] and result["status"] != "ready"


def test_node_name_mapping_does_not_alert_unrelated_cpu_nodes():
    at = 60
    data = payload([({"node": "gpu-k8s-node"}, 0), ({"node": "cpu-node"}, 0)], at)
    info = [({"node": "gpu-k8s-node", "internal_ip": "gpu-ip"}, 1)]
    result = normalize("node_ready", data, "yz", at, {("gpu-ip", "GPU-MOCK")}, info)
    assert len(result["values"]) == 1 and result["values"][0]["node"] == "gpu-ip"


def test_missing_partial_and_stale_do_not_claim_health_or_new_errors():
    assert normalize("gpu_util", payload([], 60), "yz", 60, {("n", "u")}, [])["status"] == "missing"
    metric = {
        "cluster": "yz",
        "key": "gpu_xid",
        "status": "ready",
        "values": [{"node": "n", "device_uuid": "u", "value": 31}],
        "evaluated_at": 60,
    }
    found = alerts([metric], 85)
    assert "发生时间" in found[0]["message"]
    metric["key"] = "gpu_ecc"
    assert "不代表本次新增" in alerts([metric], 85)[0]["message"]
    metric["status"] = "stale"
    assert alerts([metric], 85) == []


def test_fixed_queries_keep_scope_freshness_and_ready_condition():
    for key in CATALOG:
        expression = query("yz", "ops_" + key)
        assert 'cluster="k8s_yz_gpu_maas"' in expression
        assert "timestamp(" in expression and "<=120" in expression
    assert 'status="true"' in query("yz", "ops_node_ready")
    with pytest.raises(KeyError):
        query("yz", "http://invalid")


def test_infrastructure_does_not_recommend_components_from_empty_evidence():
    stats = Telemetry().snapshot()
    assert all(r["status"] == "insufficient" for r in assessment(stats, None))
    t = Telemetry()
    for _ in range(40):
        t.record("http", "application", "/api/v1/overview", 1)
    assert (
        next(r for r in assessment(t.snapshot(), None) if r["component"] == "共享缓存")["status"]
        == "investigate"
    )
    t.begin("upstream_active")
    t.begin("upstream_active")
    t.end("upstream_active")
    t.end("upstream_active")
    assert t.snapshot()["concurrency"]["upstream_active"] == {"current": 0, "peak": 2}


async def test_collector_is_bounded_database_reads_never_query_source(context):
    store, source, history, *_ = context
    service = MetricsService(history, source.settings)
    async with store.pool.connection() as conn:
        await conn.execute("TRUNCATE operations_metric_snapshot")
    await service.collect("yz")
    await service.collect("zw")

    async def forbidden(*args, **kwargs):
        raise AssertionError("page contacted source")

    source.range_query = forbidden
    source.instant_query = forbidden
    # A second collector respects the database cooldown for both clusters.
    await service.tick()
    metrics = await service.read(["yz"])
    assert len(metrics) == 7 and all(m["status"] == "ready" for m in metrics)
    assert alerts(metrics, 85)
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE operations_metric_snapshot SET attempted_at=now()-interval '5 minutes'")
    assert all(m["status"] == "stale" and m["values"] == [] for m in await service.read(["yz"]))
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE operations_metric_snapshot SET attempted_at=now(),evaluated_at=60")
    assert all(m["status"] == "stale" for m in await service.read(["yz"]))
    result = await infrastructure(history, Telemetry())
    assert result["database"]["database_bytes"] > 0


async def prepared_report(context, monkeypatch):
    store, source, history, a, b = context
    source.fixture["yz"][0]["allocations"][0]["value"] += 100
    await history.sync.ingest("yz", 60, b.start, b.end)
    snapshot = SnapshotService(history.resources, store)
    await snapshot.synchronize("yz", True)
    metrics = MetricsService(history, source.settings)
    await metrics.collect("yz")
    monkeypatch.setattr("app.operations.reports.windows", lambda *args: (a, b))
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("report contacted Prometheus")

    source.range_query = forbidden
    source.instant_query = forbidden
    service = ReportService(snapshot, history, metrics, source.settings)
    return service, calls


async def test_report_uses_history_service_facts_and_no_source_or_persistence(context, monkeypatch):
    service, calls = await prepared_report(context, monkeypatch)
    result = await service.generate(ReportRequest(cluster="yz"))
    change = result["clusters"][0]["comparison"]["changes"][0]
    assert change["allocated_change_raw"] == 100
    assert result["review_status"] == "draft" and result["calculation"] == "deterministic"
    assert calls == []
    service.settings.insight_allowed_clusters = ["zw"]
    with pytest.raises(SourceError):
        await service.generate(ReportRequest(cluster="yz"))


async def test_reports_and_monitoring_api_validate_scope_origin_and_body(context, monkeypatch):
    service, calls = await prepared_report(context, monkeypatch)
    # Mock lifespan has no database tasks; prepared services are read-only.
    config = service.settings.model_copy(
        update={"history_database_url": SecretStr(""), "history_sync_enabled": False}
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        app.state.operations = service
        app.state.operational_metrics = service.metrics
        response = await client.post("/api/v1/operations/reports", json={"cluster": "yz"})
        assert response.status_code == 200 and response.json()["review_status"] == "draft"
        assert (await client.post("/api/v1/operations/reports", json={"url": "bad"})).status_code == 422
        assert (
            await client.post("/api/v1/operations/reports", json={}, headers={"Origin": "https://bad"})
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/operations/reports", content="x" * 3000, headers={"Content-Type": "application/json"}
            )
        ).status_code == 413
        assert (await client.get("/api/v1/operations/monitoring?cluster=yz")).status_code == 200
        service.settings.insight_allowed_clusters = ["yz"]
        assert (await client.get("/api/v1/operations/monitoring?cluster=zw")).status_code == 403
        assert calls == []
