import os
import time

import pytest
import pytest_asyncio
from psycopg.errors import CheckViolation, ForeignKeyViolation
from pydantic import ValidationError

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.core.config import Settings
from app.history.contracts import HistoryScope
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.models.history_analysis import AnalysisWindow
from app.services.resources import ResourceService


@pytest_asyncio.fixture
async def context():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable Mock PostgreSQL database")
    settings = Settings(
        _env_file=None,
        data_mode="mock",
        prom_yz_url="",
        prom_zw_url="",
        history_database_url=url,
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
    )
    store = HistoryStore(settings)
    await store.open()
    await store.initialize()
    async with store.pool.connection() as c:
        await c.execute(
            "TRUNCATE history_rollup, history_fill_guard, history_source_state, history_sample,history_coverage,history_entity,history_progress,resource_snapshot RESTART IDENTITY"
        )
    source = MockPrometheusClient(settings)
    history = HistoryService(ResourceService(source, settings), store)
    end = int(time.time()) // 60 * 60 - 120
    baseline = AnalysisWindow(start=end - 180, end=end - 120, grain=60)
    target = AnalysisWindow(start=end - 60, end=end, grain=60)
    await history.sync.ingest("yz", 60, baseline.start, baseline.end)
    try:
        yield store, source, history, baseline, target
    finally:
        await store.close()


async def finish(context):
    _store, _source, history, baseline, target = context
    await history.sync.ingest("yz", 60, target.start, target.end)
    return history.analysis, baseline, target


async def test_cluster_node_namespace_pod_changes_reconcile(context):
    _store, source, _history, _baseline, _target = context
    gpu = source.fixture["yz"][0]
    allocation = gpu["allocations"][0]
    allocation["value"] += 100
    analysis, a, b = await finish(context)
    cluster = await analysis.compare_windows("yz", a, b)
    assert cluster.complete and cluster.changes[0].allocated_change_raw == 100
    node = await analysis.allocation_changes("yz", a, b, "node")
    assert sum(x.allocated_change_raw for x in node.changes) == 100
    namespaces = await analysis.allocation_changes(
        "yz", a, b, "namespace", HistoryScope(node_name=gpu["node"])
    )
    assert namespaces.complete
    assert sum(x.allocated_change_raw for x in namespaces.changes) == 100
    pods = await analysis.allocation_changes(
        "yz", a, b, "pod", HistoryScope(node_name=gpu["node"], namespace_name=allocation["namespace"])
    )
    assert sum(x.allocated_change_raw for x in pods.changes) == 100
    assert all(c.target.capacity_mean_raw is None for c in pods.changes)
    assert pods.warnings  # lifecycle / Pod UID limitations are explicit


async def test_capacity_change_is_separate_from_allocation_growth(context):
    _store, source, _history, _a, _b = context
    source.fixture["yz"][0]["capacity"] += 1000
    analysis, a, b = await finish(context)
    result = await analysis.compare_windows("yz", a, b)
    row = result.changes[0]
    assert row.capacity_change_raw == 1000 and row.allocated_change_raw == 0
    assert row.target.ratio_mean < row.baseline.ratio_mean


async def test_gpu_history_candidate_evidence_has_no_scheduling_claim(context):
    _store, source, _history, _a, _b = context
    gpu = source.fixture["yz"][0]
    analysis, a, b = await finish(context)
    result = await analysis.compare_windows("yz", a, b, "gpu", (gpu["node"], gpu["uuid"]))
    assert result.complete
    row = result.changes[0].target
    assert row.capacity_mean_raw == gpu["capacity"]
    assert row.allocated_max_raw == sum(x["value"] for x in gpu["allocations"])
    assert row.unallocated_min_raw == row.capacity_mean_raw - row.allocated_max_raw
    assert "utilization" not in result.model_dump_json()
    assert "schedulable" not in result.model_dump_json()


async def test_business_entry_is_zero_only_with_verified_complete_coverage(context):
    store, source, _history, _a, _b = context
    source.fixture["yz"][0]["allocations"].append(
        {"namespace": "new-business", "pod": "new-pod", "value": 200}
    )
    analysis, a, b = await finish(context)
    result = await analysis.compare_windows("yz", a, b, "namespace", ("new-business",))
    assert result.complete and result.changes[0].allocated_change_raw == 200
    assert result.changes[0].baseline.absent_points == a.expected_points
    assert result.changes[0].baseline.verified_coverage_points == a.expected_points
    assert result.changes[0].baseline.zero_inferred
    async with store.pool.connection() as c:
        await c.execute("DELETE FROM history_coverage WHERE cluster='yz' AND grain=60 AND ts=%s", (a.start,))
    result = await analysis.compare_windows("yz", a, b, "namespace", ("new-business",))
    assert not result.complete and result.changes[0].allocated_change_raw is None
    assert result.changes[0].baseline.allocated_mean_raw is None


@pytest.mark.parametrize("quality_version", [1, 2])
async def test_partial_recheck_cannot_destroy_good_samples(context, quality_version):
    store, source, history, a, _b = context
    async with store.pool.connection() as c:
        await c.execute("UPDATE history_coverage SET quality_version=%s", (quality_version,))
    before = await store.read("yz", 60, a.start, a.end, 60)
    # Successful but invalid numeric input is degraded, not a replacement for known good history.
    original = source.range_query

    async def partial(*args, **kwargs):
        payload = await original(*args, **kwargs)
        if args[1] == "device_capacity":
            payload["data"]["result"][0]["values"][0][1] = "NaN"
        return payload

    source.range_query = partial
    await history.sync.ingest("yz", 60, a.start, a.end)
    after = await store.read("yz", 60, a.start, a.end, 60)
    assert after[0][1:3] == before[0][1:3]
    assert after[0][3] == "degraded"
    assert "最新复查不完整，保留已验证历史" in after[0][4]


async def test_duplicate_business_identity_does_not_double_count(context):
    store, source, history, _a, b = context
    allocation = source.fixture["yz"][0]["allocations"][0]
    source.fixture["yz"][0]["allocations"].append(dict(allocation))
    original = source.instant_query

    async def replicas(cluster, template, at):
        result = await original(cluster, template, at)
        if template == "workloads":
            for index, row in enumerate(result["data"]["result"]):
                row["metric"]["instance"] = f"replica-{index}"
        return result

    source.instant_query = replicas
    await history.sync.ingest("yz", 60, b.start, b.end)
    rows = await store.read("yz", 60, b.start, b.end, 60)
    assert rows[0][2] is None and rows[0][3] == "degraded"
    assert any("身份重复" in note for note in rows[0][4])
    result = await history.analysis.allocation_changes("yz", context[3], b)
    assert not result.complete
    assert all(x.allocated_change_raw is None for x in result.changes)


async def test_constraints_prevent_cross_cluster_and_malformed_entities(context):
    store, _source, _history, _a, _b = context
    with pytest.raises(CheckViolation):
        async with store.pool.connection() as c:
            await c.execute(
                "INSERT INTO history_entity(cluster,dimension,entity_key) VALUES ('yz','gpu',ARRAY['node-only'])"
            )
    with pytest.raises(ForeignKeyViolation):
        async with store.pool.connection() as c:
            await c.execute(
                "INSERT INTO history_sample(cluster,grain,ts,dimension,capacity,allocated,status,warnings,entity_id) VALUES ('yz',60,1800000000,'gpu',1,1,'healthy','[]',999999999)"
            )
    with pytest.raises(SourceError):
        await store.read("yz", 60, context[3].start, context[3].end, 60, "gpu", ("node-only",))


async def test_analysis_is_database_only_and_rejects_unbounded_scopes(context):
    _store, source, _history, _a, _b = context
    analysis, a, b = await finish(context)

    async def forbidden(*args, **kwargs):
        raise AssertionError("analysis must never request Prometheus")

    source.range_query = source.instant_query = forbidden
    assert (await analysis.compare_windows("yz", a, b)).complete
    with pytest.raises(SourceError):
        await analysis.allocation_changes("yz", a, b, "pod", top_k=10000)
    with pytest.raises(SourceError):
        await analysis.compare_windows("yz", a, b, "gpu", ("node", "uuid"), HistoryScope(namespace_name="ns"))
    with pytest.raises(ValidationError):
        HistoryScope(sql="DROP TABLE history_sample")
    with pytest.raises(ValidationError):
        AnalysisWindow(start=a.start, end=a.end + 1, grain=60)


async def test_tail_failure_does_not_starve_older_backfill(context):
    store, source, history, _a, _b = context
    original = source.range_query
    now = int(time.time())

    async def fail_tail(cluster, template, start, end, step, *args):
        if end > now - 8000:
            raise SourceError("tail fixture failure")
        return await original(cluster, template, start, end, step, *args)

    source.range_query = fail_tail
    await history.sync.tick(budget=1, now=now)
    assert await store.progress("yz", 60) is not None
    assert history.sync.last_status == "partial"


async def test_history_stale_raw_samples_are_not_valid_agent_evidence(context):
    store, source, history, a, b = context
    source.settings.mock_scenario = "stale"
    await history.sync.ingest("yz", 60, b.start, b.end)
    rows = await store.read("yz", 60, b.start, b.end, 60)
    assert rows[0][3] == "degraded"
    assert any("过期" in note for note in rows[0][4])
    result = await history.analysis.compare_windows("yz", a, b)
    assert not result.complete and result.changes[0].allocated_change_raw is None


async def test_workload_only_gpu_keeps_historical_drilldown(context):
    store, source, history, _a, b = context
    missing = source.fixture["yz"][0]
    original = source.range_query

    async def drop_inventory(cluster, template, *args, **kwargs):
        result = await original(cluster, template, *args, **kwargs)
        if template in ("device_capacity", "device_allocated"):
            result["data"]["result"] = [
                r for r in result["data"]["result"] if r["metric"]["device_uuid"] != missing["uuid"]
            ]
        return result

    source.range_query = drop_inventory
    await history.sync.ingest("yz", 60, b.start, b.end)
    rows = await store.read("yz", 60, b.start, b.end, 60, "gpu", (missing["node"], missing["uuid"]))
    assert rows and rows[0][1] is None and rows[0][2] is None
    entities = await history.entities("yz", 60, b.start, b.end, "workload")
    assert any(e[0] == missing["node"] and e[1] == missing["uuid"] for e in entities["entities"])


async def test_unknown_entity_is_not_fabricated_zero(context):
    _store, _source, _history, _a, _b = context
    analysis, a, b = await finish(context)
    result = await analysis.compare_windows("yz", a, b, "namespace", ("does-not-exist",))
    assert not result.complete
    assert result.changes[0].allocated_change_raw is None


async def test_entity_values_and_coverage_share_mvcc_snapshot(context):
    import asyncio

    store, source, _history, a, _b = context
    gpu = source.fixture["yz"][0]
    original = store._read
    ready, release = asyncio.Event(), asyncio.Event()
    first = True

    async def paused(*args, **kwargs):
        nonlocal first
        rows = await original(*args, **kwargs)
        if first:
            first = False
            ready.set()
            await release.wait()
        return rows

    store._read = paused
    task = asyncio.create_task(
        store.read_bundle("yz", 60, a.start, a.end, 60, "gpu", (gpu["node"], gpu["uuid"]))
    )
    await asyncio.wait_for(ready.wait(), 2)
    async with store.pool.connection() as c:
        await c.execute("UPDATE history_coverage SET last_attempt_status='degraded' WHERE cluster='yz'")
    release.set()
    rows, coverage = await task
    assert rows[0][3] == coverage[0][3] == "healthy"
    store._read = original
    assert (await store.read("yz", 60, a.start, a.end, 60))[0][3] == "degraded"


async def test_unknown_units_cannot_produce_ratios_or_remaining_capacity(context):
    _store, source, _history, _a, _b = context
    analysis, a, b = await finish(context)
    source.settings.memory_capacity_unit = "unverified"
    source.settings.memory_allocation_unit = "unverified"
    # Unit contract changes require another database in production; exercise presentation policy only.
    result = await analysis.compare_windows("yz", a, b)
    assert result.changes[0].target.ratio_mean is None
    assert result.changes[0].target.unallocated_min_raw is None
    assert result.warnings


async def test_analysis_driver_errors_are_sanitized(context):
    from psycopg import OperationalError

    _store, _source, history, a, b = context

    async def failure(*args):
        raise OperationalError("private-fixture-content")

    history.store.analysis_period = failure
    with pytest.raises(SourceError) as exc:
        await history.analysis.compare_windows("yz", a, b)
    assert "private-fixture-content" not in str(exc.value)


async def test_all_constraints_can_be_validated_without_modifying_samples(context):
    store, _source, _history, _a, _b = context
    async with store.pool.connection() as c:
        before = (await (await c.execute("SELECT count(*) FROM history_sample")).fetchone())[0]
    await store.validate_constraints()
    async with store.pool.connection() as c:
        after = (await (await c.execute("SELECT count(*) FROM history_sample")).fetchone())[0]
        assert not await (
            await c.execute(
                "SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace AND NOT convalidated"
            )
        ).fetchall()
    assert before == after


async def test_top_k_exposes_complete_cohort_instead_of_hiding_other_contributions(context):
    _store, source, _history, _a, _b = context
    source.fixture["yz"][0]["allocations"][0]["value"] += 100
    source.fixture["yz"][0]["allocations"].append(
        {"namespace": "another-business", "pod": "another-pod", "value": 200}
    )
    analysis, a, b = await finish(context)
    result = await analysis.allocation_changes("yz", a, b, top_k=1)
    assert result.complete and len(result.changes) == 1
    assert result.changes[0].allocated_change_raw == 200
    assert result.cohort_allocated_change_raw == 300
    assert result.omitted_entities == result.entity_count - 1


async def test_disjoint_business_populations_cannot_exceed_union_budget(context):
    _store, source, history, a, b = context
    source.fixture["yz"][0]["allocations"] = [
        {"namespace": f"old-{i}", "pod": f"pod-{i}", "value": 1} for i in range(600)
    ]
    await history.sync.ingest("yz", 60, a.start, a.end)
    source.fixture["yz"][0]["allocations"] = [
        {"namespace": f"new-{i}", "pod": f"pod-{i}", "value": 1} for i in range(600)
    ]
    await history.sync.ingest("yz", 60, b.start, b.end)
    result = await history.analysis.allocation_changes("yz", a, b)
    assert result.truncated and not result.complete
    assert result.entity_count is None and result.changes == []
    assert any("候选" in note for note in result.warnings)
