"""Golden cases for low-use investigation, source semantics and persisted evidence."""

import copy
import os
import time

import httpx
import pytest
import pytest_asyncio
from psycopg import Error as DatabaseError

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.core.config import Settings
from app.efficiency.normalize import normalize
from app.efficiency.queries import METRICS, efficiency_query
from app.efficiency.service import EfficiencyRequest, EfficiencyService, investigate
from app.efficiency.store import PROFILE
from app.history.service import HistoryService
from app.history.store import HistoryStore
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


async def fixture_rows(config=None):
    config = config or settings()
    source = MockPrometheusClient(config)
    now = int(time.time())
    end = (now - 120) // 60 * 60
    start = end - 3600
    payloads = {name: await source.range_query("yz", name, start, end, 60) for name in METRICS}
    return source, now, start, end, payloads, normalize(payloads, "yz", start, end, config)


def evidence(rows, start, end):
    coverage = [(ts, "healthy", "healthy", end, [], PROFILE) for ts in range(start, end + 1, 60)]
    devices = []
    workloads = []
    for r in rows:
        devices.append(
            (
                r["ts"],
                r["node"],
                r["device_uuid"],
                r["capacity_raw"],
                r["allocated_raw"],
                r["allocation_unit"],
                r["utilization_pct"],
                r["physical_memory_bytes"],
                r["verified"],
                r["warnings"],
            )
        )
        for w in r["workloads"]:
            workloads.append(
                (
                    r["ts"],
                    r["node"],
                    r["device_uuid"],
                    w["namespace_name"],
                    w["pod_name"],
                    w["container_name"],
                    w["pod_uid"],
                    w["allocated_raw"],
                    w["pod_started_at"],
                    w["running"],
                    w["restart_count"],
                    w["verified"],
                )
            )
    return coverage, devices, workloads


async def test_steady_low_use_has_lifecycle_evidence():
    _, _, start, end, _, rows = await fixture_rows()
    result = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))
    item = result[0]
    assert item["classification"] == "review_candidate"
    assert item["sustained_minutes"] == 60
    assert item["valid_points"] == item["expected_points"] == 61
    assert item["workloads"][0]["pod_uid"].startswith("UID-")
    assert item["workloads"][0]["namespace"] != "exporter-system"


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("high_compute", "active_compute"),
        ("memory_resident", "memory_resident"),
        ("startup", "lifecycle_or_gaps"),
        ("pending", "lifecycle_or_gaps"),
        ("restart", "lifecycle_or_gaps"),
        ("uid_change", "lifecycle_or_gaps"),
        ("intermittent", "active_compute"),
        ("intermittent_window", "no_sustained_low_use"),
        ("gap", "lifecycle_or_gaps"),
        ("unknown_unit", "insufficient"),
        ("no_allocation", "below_allocation_threshold"),
    ],
)
async def test_counterexamples_are_not_waste(scenario, expected):
    _, _, start, end, _, rows = await fixture_rows()
    rows = [r for r in rows if r["device_uuid"] == rows[0]["device_uuid"]]
    for r in rows:
        if scenario == "high_compute":
            r["utilization_pct"] = 80
        if scenario == "memory_resident":
            r["physical_memory_bytes"] = 12e9
        if scenario == "startup":
            for w in r["workloads"]:
                w["pod_started_at"] = end - 600
        if scenario == "pending":
            for w in r["workloads"]:
                w["running"] = False
        if scenario == "restart" and r["ts"] >= end - 600:
            for w in r["workloads"]:
                w["restart_count"] = 1
        if scenario == "uid_change" and r["ts"] >= end - 600:
            for w in r["workloads"]:
                w["pod_uid"] = "new-uid"
        if scenario == "intermittent" and (r["ts"] - start) // 60 % 20 == 0:
            r["utilization_pct"] = 80
        if scenario == "intermittent_window" and (r["ts"] - start) // 60 % 20 == 10:
            r["utilization_pct"] = 80
        if scenario == "gap" and r["ts"] == end - 600:
            r["verified"] = False
        if scenario == "unknown_unit":
            r["allocation_unit"] = "unverified"
        if scenario == "no_allocation":
            r["allocated_raw"] = 0
            r["workloads"] = []
    item = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert item["classification"] == expected
    if scenario == "memory_resident":
        assert item["classification"] == "memory_resident"
    if scenario == "high_compute":
        assert item["classification"] == "active_compute"
    if scenario == "no_allocation":
        assert item["classification"] == "below_allocation_threshold"


async def test_consecutive_intervals_and_coverage_not_sample_count():
    _, _, start, end, _, rows = await fixture_rows()
    uuid = rows[0]["device_uuid"]
    rows = [r for r in rows if r["device_uuid"] == uuid]
    for r in rows:
        if r["ts"] < end - 29 * 60:
            r["utilization_pct"] = 80
    item = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert item["sustained_minutes"] == 29
    assert item["classification"] != "review_candidate"
    rows = rows[20:]
    item = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert item["classification"] == "insufficient"


async def test_aliases_duplicates_and_conflicting_actual_usage():
    source, _, start, end, payloads, rows = await fixture_rows()
    for r in payloads["eff_allocated"]["data"]["result"]:
        r["metric"]["deviceuuid"] = r["metric"].pop("device_uuid")
    duplicate = copy.deepcopy(payloads["eff_allocated"]["data"]["result"][0])
    payloads["eff_allocated"]["data"]["result"].append(duplicate)
    aliased = normalize(payloads, "yz", start, end, source.settings)
    assert aliased[0]["allocated_raw"] == rows[0]["allocated_raw"]
    duplicate = copy.deepcopy(payloads["eff_utilization"]["data"]["result"][0])
    duplicate["values"][0][1] = "90"
    payloads["eff_utilization"]["data"]["result"].append(duplicate)
    assert normalize(payloads, "yz", start, end, source.settings)[0]["verified"] is False


@pytest.mark.parametrize("problem", ["partial", "cluster", "nan", "grid"])
async def test_untrusted_source_is_rejected(problem):
    source, _, start, end, payloads, _ = await fixture_rows()
    if problem == "partial":
        payloads["eff_memory"]["warnings"] = ["partial"]
    if problem == "cluster":
        payloads["eff_memory"]["data"]["result"][0]["metric"]["cluster"] = "foreign"
    if problem == "nan":
        payloads["eff_memory"]["data"]["result"][0]["values"][0][1] = "NaN"
    if problem == "grid":
        payloads["eff_memory"]["data"]["result"][0]["values"][0][0] += 1
    with pytest.raises(SourceError):
        normalize(payloads, "yz", start, end, source.settings)


def test_templates_are_scoped_fresh_and_do_not_use_container_zero():
    for name in METRICS:
        query = efficiency_query("yz", name)
        assert 'cluster="k8s_yz_gpu_maas"' in query
        assert "timestamp(" in query
        assert "hami_container_device_utilization_ratio" not in query
    assert "namespace_name" in efficiency_query("yz", "eff_pod_info")


@pytest_asyncio.fixture
async def stored():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable Mock PostgreSQL")
    config = settings(history_database_url=url, efficiency_enabled=True)
    store = HistoryStore(config)
    await store.open()
    await store.initialize()
    async with store.pool.connection() as c:
        await c.execute(
            "TRUNCATE efficiency_review,efficiency_case,efficiency_investigation,efficiency_container_sample,efficiency_workload_sample,efficiency_device_sample,efficiency_coverage"
        )
    source = MockPrometheusClient(config)
    history = HistoryService(ResourceService(source, config), store)
    service = EfficiencyService(history, config)
    try:
        yield config, store, source, service
    finally:
        await store.close()


async def collect_hour(service, cluster="yz", now=None):
    now = int(time.time()) if now is None else now
    end = (now - 120) // 60 * 60
    for start in range(end - 3600, end + 1, 1800):
        await service.sync.ingest(cluster, start, min(start + 1740, end))
    return now, end


async def test_database_only_and_idempotent(stored):
    _, db, source, service = stored
    now, end = await collect_hour(service)
    await collect_hour(service, now=now)
    async with db.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM efficiency_coverage")).fetchone())[0] == 61

    async def forbidden(*args, **kwargs):
        raise AssertionError("investigation must not query Prometheus")

    source.range_query = forbidden
    source.instant_query = forbidden
    report = await service.evaluate(EfficiencyRequest(cluster="yz"), now)
    assert report["clusters"][0]["candidate_count"] > 0
    assert report["results"][0]["classification"] == "review_candidate"
    assert report["end"] == end


async def test_failed_recheck_preserves_values_but_limits_findings(stored):
    _, _, _, service = stored
    now, end = await collect_hour(service)
    before = await service.store.read("yz", end, end)
    await service.store.write("yz", end, end, [], "source unavailable")
    after = await service.store.read("yz", end, end)
    assert before[1:] == after[1:]
    assert after[0][0][2] == "unavailable"
    report = await service.evaluate(EfficiencyRequest(cluster="yz"), now)
    assert report["clusters"][0]["candidate_count"] == 0
    assert all(item["classification"] == "insufficient" for item in report["results"])


async def test_write_rollback_and_retention_cascade(stored):
    _, db, _, service = stored
    _, _now, _start, end, _, rows = await fixture_rows()
    rows = [r for r in rows if r["ts"] == end]
    broken = copy.deepcopy(rows)
    broken[0]["workloads"][0]["allocated_raw"] = -1
    with pytest.raises(DatabaseError):
        await service.store.write("yz", end, end, broken)
    assert (await service.store.read("yz", end, end)) == ([], [], [])
    await service.store.write("yz", end, end, rows)
    await service.store.prune(end + 2 * 86400 + 1)
    assert (await service.store.read("yz", end, end)) == ([], [], [])
    # Existing quota schema/data remains available after independent usage retention.
    await db.validate()


async def test_scheduler_coalesces_recent_tail_across_instances(stored):
    _, _, _, service = stored
    now = int(time.time())
    await service.sync.tick(now)
    before = await service.store.read("yz", (now - 120) // 60 * 60, (now - 120) // 60 * 60)

    async def forbidden(*args, **kwargs):
        raise AssertionError("duplicate collection")

    service.sync.ingest = forbidden
    await service.sync.tick(now)
    assert before[0]


async def test_api_guards_and_database_result(stored):
    config, _, _, service = stored
    await collect_hour(service)
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        assert not (await client.get("/api/v1/efficiency/capabilities")).json()["available"]
        assert (await client.post("/api/v1/efficiency/investigate", json={})).status_code == 503
        app.state.efficiency = service
        assert (
            await client.post("/api/v1/efficiency/investigate", json={"cluster": "yz"})
        ).status_code == 200
        assert (await client.post("/api/v1/efficiency/investigate", json={"hours": 168})).status_code == 422
        assert (
            await client.post("/api/v1/efficiency/investigate", json={"url": "http://bad"})
        ).status_code == 422
        assert (
            await client.post(
                "/api/v1/efficiency/investigate", json={}, headers={"Origin": "https://foreign.example"}
            )
        ).status_code == 403
        config.insight_allowed_clusters = ["yz"]
        assert (
            await client.post("/api/v1/efficiency/investigate", json={"cluster": "zw"})
        ).status_code == 403


async def test_schema_four_upgrade_preserves_quota_history(stored):
    _, db, _, service = stored
    ts = (int(time.time()) - 120) // 60 * 60
    await service.history.sync.ingest("yz", 60, ts, ts)
    before = await db.read("yz", 60, ts, ts, 60)
    async with db.pool.connection() as c:
        await c.execute(
            "DROP TABLE agent_task_memory,workload_resource_snapshot,host_metric_bucket,host_history_coverage,operations_decision,operations_record,operations_issue_event,operations_issue,node_metric_snapshot,investigation_feedback,investigation_event,investigation_task,efficiency_review,efficiency_case,efficiency_investigation,efficiency_container_sample,efficiency_workload_sample,efficiency_device_sample,efficiency_coverage"
        )
        await c.execute("UPDATE history_metadata SET version=4 WHERE id=1")
    await db.initialize()
    await db.initialize()
    assert await db.read("yz", 60, ts, ts, 60) == before
    assert await service.store.read("yz", ts, ts) == ([], [], [])


async def test_uuid_ambiguity_never_attributes_host_usage_to_two_nodes():
    source, _, start, end, payloads, _ = await fixture_rows()
    duplicate = copy.deepcopy(payloads["eff_capacity"]["data"]["result"][0])
    duplicate["metric"]["node"] = "other-node"
    payloads["eff_capacity"]["data"]["result"].append(duplicate)
    rows = normalize(payloads, "yz", start, end, source.settings)
    uuid = duplicate["metric"]["device_uuid"]
    assert all(not r["verified"] and r["utilization_pct"] is None for r in rows if r["device_uuid"] == uuid)


async def test_same_pod_name_with_ambiguous_uids_is_not_assumed_same_task():
    source, _, start, end, payloads, _ = await fixture_rows()
    duplicate = copy.deepcopy(payloads["eff_pod_info"]["data"]["result"][0])
    duplicate["metric"]["uid"] = "reused-name-new-uid"
    payloads["eff_pod_info"]["data"]["result"].append(duplicate)
    rows = normalize(payloads, "yz", start, end, source.settings)
    assert not rows[0]["verified"]
    assert rows[0]["workloads"][0]["pod_uid"] is None


async def test_usage_mean_keeps_active_samples_when_lifecycle_is_missing():
    _, _, start, end, _, rows = await fixture_rows()
    rows = [r for r in rows if r["device_uuid"] == rows[0]["device_uuid"]]
    for index, row in enumerate(rows):
        row["utilization_pct"] = 0 if index < 30 else 80
        if index >= 30:
            row["verified"] = False
            for workload in row["workloads"]:
                workload["verified"] = False
    result = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert result["usage_valid_points"] == 61
    assert result["mean_utilization_pct"] == pytest.approx(80 * 31 / 61)
    assert result["max_utilization_pct"] == 80
    assert result["screening_status"] == "above_threshold"
    assert result["classification"] != "review_candidate"


async def test_usage_screening_uses_mean_and_allocation_without_hidden_memory_threshold():
    _, _, start, end, _, rows = await fixture_rows()
    rows = [r for r in rows if r["device_uuid"] == rows[0]["device_uuid"]]
    for row in rows:
        row["utilization_pct"] = 0.04
        row["physical_memory_bytes"] = 20e9
    result = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert result["mean_utilization_pct"] == pytest.approx(0.04)
    assert result["screening_status"] == "low_utilization"
    assert result["classification"] == "memory_resident"  # Screening is not a waste verdict.
    result = investigate(
        "yz", EfficiencyRequest(utilization_threshold=0), start, end, *evidence(rows, start, end)
    )[0]
    assert result["screening_status"] == "above_threshold"


async def test_usage_missing_points_and_failed_recheck_do_not_become_zero():
    _, _, start, end, _, rows = await fixture_rows()
    rows = [r for r in rows if r["device_uuid"] == rows[0]["device_uuid"]]
    for row in rows[:10]:
        row["utilization_pct"] = None
    result = investigate("yz", EfficiencyRequest(), start, end, *evidence(rows, start, end))[0]
    assert result["usage_valid_points"] == 51
    assert result["mean_utilization_pct"] is None
    assert result["screening_status"] == "insufficient"
    coverage, devices, workloads = evidence(rows, start, end)
    coverage[-1] = (*coverage[-1][:2], "unavailable", *coverage[-1][3:])
    result = investigate("yz", EfficiencyRequest(), start, end, coverage, devices, workloads)[0]
    assert result["current_allocated_raw"] is None
    assert result["mean_utilization_pct"] is None


async def test_dcgm_uuid_alias_and_mib_conversion_match_resource_metrics():
    source, _, start, end, payloads, _ = await fixture_rows()
    for name in ("eff_utilization", "eff_memory"):
        for row in payloads[name]["data"]["result"]:
            row["metric"]["UUID"] = row["metric"].pop("device_uuid")
    rows = normalize(payloads, "yz", start, end, source.settings)
    raw = await source.range_query("yz", "ops_gpu_memory", end, end, 60)
    expected = {r["metric"]["UUID"]: float(r["values"][0][1]) * 2**20 for r in raw["data"]["result"]}
    assert all(r["physical_memory_bytes"] == expected[r["device_uuid"]] for r in rows)
    for series in payloads["eff_memory"]["data"]["result"]:
        for point in series["values"]:
            point[1] = "9223372036854775807"
    assert all(
        r["physical_memory_bytes"] is None for r in normalize(payloads, "yz", start, end, source.settings)
    )
