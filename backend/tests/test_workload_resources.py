# ruff: noqa: F811
import copy
import time

import httpx
from pydantic import SecretStr
from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.main import create_app
from app.workloads.normalize import normalize
from app.workloads.queries import SOURCES, query
from app.workloads.service import WorkloadService, collect, mock


def data():
    fixture = {"yz": [{"node": "gpu-node", "allocations": [{"namespace": "business", "pod": "model-one"}]}]}
    inventory = {("gpu-node", "MOCK-GPU")}
    info = [({"node": "gpu-node", "internal_ip": "gpu-node"}, 1)]
    return fixture, inventory, info, {t: mock(fixture, "yz", t, 120, 120, 60) for t in SOURCES}


def test_units_runtime_identity_and_missing_resources_are_separate():
    _, inv, info, payloads = data()
    row = normalize("yz", 120, inv, info, payloads)["rows"][0]
    assert row["cpu_usage_cores"] == 0.25 and row["memory_working_set_bytes"] == 2e9
    assert row["cpu_request_cores"] == 1 and row["cpu_limit_cores"] == 2
    assert row["namespace_name"] == "business"
    payloads["wl_requests"]["data"]["result"] = []
    row = normalize("yz", 120, inv, info, payloads)["rows"][0]
    assert row["cpu_request_cores"] is None and row["metric_statuses"]["cpu_request_cores"] == "missing"
    assert row["cpu_usage_cores"] == 0.25
    assert "rate(" in query("yz", "wl_cpu") and "on(namespace,pod)" in query("yz", "wl_cpu")
    assert "__no_verified_gpu_node__" in query("yz", "wl_pods")


def test_old_container_id_foreign_cluster_conflicts_and_units_do_not_leak_values():
    _, inv, info, payloads = data()
    payloads["wl_cpu"]["data"]["result"][0]["metric"]["id"] = "/old/" + 64 * "a"
    payloads["wl_memory"]["data"]["result"][0]["metric"]["cluster"] = "foreign"
    payloads["wl_requests"]["data"]["result"][0]["metric"]["unit"] = "byte"
    report = normalize("yz", 120, inv, info, payloads)
    row = report["rows"][0]
    assert report["status"] == "partial" and row["cpu_usage_cores"] is None
    assert row["memory_working_set_bytes"] is None and row["cpu_request_cores"] is None
    assert row["metric_statuses"]["cpu_request_cores"] == "unit_conflict"
    _, inv, info, payloads = data()
    duplicate = copy.deepcopy(payloads["wl_containers"]["data"]["result"][0])
    duplicate["metric"]["container_id"] = "containerd://" + 64 * "b"
    payloads["wl_containers"]["data"]["result"].append(duplicate)
    report = normalize("yz", 120, inv, info, payloads)
    assert report["identity_rejected"] == 1 and not report["rows"]


async def test_current_snapshots_dedup_db_only_summary_models_and_stale(domain):
    store, source, h, _, _ = domain
    source.settings.workload_metrics_enabled = True
    fixture, inv, info, _ = data()
    source.fixture = fixture
    at = int(time.time()) // 60 * 60
    await collect(h, "yz", at, inv, info)
    await collect(h, "yz", at, inv, info)
    service = WorkloadService(h, source.settings)

    async def forbidden(*a, **kw):
        raise AssertionError("page read Prometheus")

    source.range_query = forbidden
    result = await service.read("yz")
    assert len(result["containers"]) == 1 and result["summary"]["cpu_usage_cores"] == 0.25
    assert result["containers"][0]["model_key"] is None
    async with store.pool.connection() as c:
        assert (
            await (
                await c.execute("SELECT count(*) FROM workload_resource_snapshot WHERE cluster='yz'")
            ).fetchone()
        )[0] == 1
        await c.execute(
            "UPDATE workload_resource_snapshot SET evaluated_at=%s WHERE cluster='yz'", (at - 3600,)
        )
    result = await service.read("yz")
    assert not result["containers"] and result["sources"][0]["status"] == "stale"
    assert all(v is None for v in result["summary"].values())


async def test_mapping_and_faults_do_not_fabricate_aggregate(domain):
    store, source, h, _, _ = domain
    source.settings.workload_metrics_enabled = True
    fixture, inv, info, _ = data()
    source.fixture = fixture
    at = int(time.time()) // 60 * 60
    await collect(h, "yz", at, inv, info)
    async with store.pool.connection() as c:
        await c.execute(
            "INSERT INTO resource_business_model VALUES('workload-test','显式业务模型') ON CONFLICT DO NOTHING"
        )
        await c.execute(
            "INSERT INTO resource_model_binding VALUES('yz','business','model-one','worker','workload-test',%s,NULL)",
            (at - 60,),
        )
    report = await WorkloadService(h, source.settings).read("yz")
    assert report["containers"][0]["model_name"] == "显式业务模型"
    original = source.range_query

    async def partial(cid, template, *a, **kw):
        if template == "wl_cpu":
            raise RuntimeError("PRIVATE FAILURE")
        return await original(cid, template, *a, **kw)

    source.range_query = partial
    await collect(h, "yz", at, inv, info)
    report = await WorkloadService(h, source.settings).read("yz")
    assert report["sources"][0]["status"] == "partial" and report["summary"]["cpu_usage_cores"] is None
    assert report["summary"]["memory_working_set_bytes"] == 2e9
    assert "PRIVATE" not in str(report)


async def test_workload_api_scope_origin_limits_and_config(domain):
    _store, source, h, _, _ = domain
    source.settings.workload_metrics_enabled = True
    fixture, inv, info, _ = data()
    source.fixture = fixture
    await collect(h, "yz", int(time.time()) // 60 * 60, inv, info)
    config = source.settings.model_copy(
        update={"history_database_url": SecretStr(""), "workload_metrics_enabled": False}
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        app.state.workloads = WorkloadService(h, source.settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            assert (
                await client.post("/api/v1/resource-statistics/workloads", json={"cluster": "yz"})
            ).status_code == 200
            assert (
                await client.post(
                    "/api/v1/resource-statistics/workloads", json={"cluster": "yz", "promql": "arbitrary"}
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/api/v1/resource-statistics/workloads",
                    json={"cluster": "yz"},
                    headers={"Origin": "https://foreign.example"},
                )
            ).status_code == 403
            source.settings.insight_allowed_clusters = ["zw"]
            assert (
                await client.post("/api/v1/resource-statistics/workloads", json={"cluster": "yz"})
            ).status_code == 403


async def test_schema_eleven_upgrade_preserves_host_facts_and_classifies_legacy_attempts(domain):
    store, source, _h, metrics, _ = domain
    source.settings.host_metrics_enabled = True
    await metrics.collect("yz")
    async with store.pool.connection() as c:
        before = (await (await c.execute("SELECT count(*) FROM host_metric_bucket")).fetchone())[0]
        await c.execute("DROP TABLE workload_resource_snapshot")
        await c.execute(
            "ALTER TABLE host_history_coverage DROP COLUMN attempt_status,DROP COLUMN failure_code"
        )
        await c.execute("UPDATE history_metadata SET version=11 WHERE id=1")
    await store.initialize()
    await store.initialize()
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM host_metric_bucket")).fetchone())[0] == before
        assert (
            await (
                await c.execute(
                    "SELECT count(*) FROM host_history_coverage WHERE complete AND attempt_status<>'ready'"
                )
            ).fetchone()
        )[0] == 0
