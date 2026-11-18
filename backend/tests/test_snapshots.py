import asyncio
import os
import time

import httpx
import pytest
import pytest_asyncio

from app.clients.prometheus import MockPrometheusClient
from app.core.config import Settings
from app.history.snapshots import SnapshotService, SnapshotUnavailable
from app.history.store import HistoryStore
from app.main import create_app
from app.services.resources import ResourceService


@pytest_asyncio.fixture
async def stored():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable Mock PostgreSQL database")
    config = Settings(
        _env_file=None,
        snapshot_refresh_min_interval_seconds=0,
        data_mode="mock",
        prom_yz_url="",
        prom_zw_url="",
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
        history_database_url=url,
    )
    store = HistoryStore(config)
    await store.open()
    await store.initialize()
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE history_rollup, history_fill_guard, history_source_state, history_sample, history_progress, resource_snapshot, history_coverage, history_entity RESTART IDENTITY"
        )
    source = MockPrometheusClient(config)
    service = SnapshotService(ResourceService(source, config), store)
    try:
        yield config, store, source, service
    finally:
        await store.close()


async def test_snapshot_reads_postgres_after_restart_without_prometheus(stored):
    config, store, source, service = stored
    expected = await service.overview(refresh=True)

    async def forbidden(*args):
        raise AssertionError("fresh database reads must not access Prometheus")

    source.instant_query = forbidden
    restarted = SnapshotService(ResourceService(source, config), store)
    result = await restarted.overview()
    assert result.totals == expected.totals
    assert all(c.storage_backend == "postgresql" for c in result.clusters)
    async with store.pool.connection() as conn:
        row = await store.snapshot("yz", conn)
        payload = row[0]
        payload["label"] = "database-proof"
        await store.save_snapshot(conn, "yz", payload, row[1], time.time())
    assert (await restarted.overview("yz")).clusters[0].label == "database-proof"


async def test_refresh_persists_new_values_then_reads_them(stored):
    _config, store, source, service = stored
    before = await service.overview(refresh=True)
    source.fixture["yz"][0]["allocations"][0]["value"] += 100
    after = await service.overview("yz", refresh=True)
    assert after.clusters[0].allocated_raw == before.clusters[0].allocated_raw + 100
    row = await store.snapshot("yz")
    assert row[0]["allocated_raw"] == after.clusters[0].allocated_raw
    # A single-cluster refresh must not update the other cluster.
    assert (await store.snapshot("zw"))[0]["fetched_at"] == before.clusters[1].fetched_at


async def test_source_failure_is_persisted_and_old_good_is_stale(stored):
    config, store, _source, service = stored
    before = await service.overview(refresh=True)
    config.mock_scenario = "partial"
    after = await service.overview(refresh=True)
    assert after.clusters[1].status == "stale"
    assert after.clusters[1].gpus == before.clusters[1].gpus
    assert after.totals.gpu_count_observed is None
    assert not after.complete
    row = await store.snapshot("zw")
    assert row[0]["status"] == "unavailable"
    assert row[1]["status"] == "healthy"
    # Old good snapshots beyond the retention window must not be returned.
    row[1]["fetched_at"] = time.time() - 301
    async with store.pool.connection() as conn:
        await store.save_snapshot(conn, "zw", row[0], row[1], time.time())
    assert (await service.overview("zw", refresh=True)).clusters[0].status == "unavailable"


async def test_snapshot_age_uses_collected_and_source_time(stored):
    _config, store, _source, service = stored
    await service.overview("yz", refresh=True)
    row = await store.snapshot("yz")
    payload = row[0]
    payload["source_sampled_at"] = time.time() - 180
    async with store.pool.connection() as conn:
        await store.save_snapshot(conn, "yz", payload, row[1], time.time())
    result = await service.overview("yz")
    assert result.clusters[0].status == "stale"
    assert result.clusters[0].source_age_seconds >= 180
    assert result.totals.capacity_raw is None


async def test_write_failure_never_returns_unpersisted_prometheus_data(stored):
    _config, store, _source, service = stored
    await service.overview(refresh=True)

    async def fail(*args):
        raise OSError("private database details")

    store.save_snapshot = fail
    with pytest.raises(SnapshotUnavailable, match="未返回未经落库"):
        await service.overview(refresh=True)


async def test_overlapping_refreshes_coalesce(stored):
    _config, _store, _source, service = stored
    original = service.collector._fetch_cluster
    calls = 0

    async def slow(*args):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return await original(*args)

    service.collector._fetch_cluster = slow
    requested = time.time()
    values = await asyncio.gather(*(service.cluster("yz", requested, True) for _ in range(10)))
    assert calls == 1
    assert all(v.storage_backend == "postgresql" for v in values)


async def test_api_endpoints_use_stored_data_and_storage_failure_is_503(stored):
    config, _store, source, _service = stored
    app = create_app(config, source)
    async with app.router.lifespan_context(app):

        async def forbidden(*args):
            raise AssertionError("ordinary API reads must use the database")

        source.instant_query = forbidden
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in ("overview", "clusters", "nodes", "gpus", "workloads"):
                response = await client.get("/api/v1/" + path)
                assert response.status_code == 200

            async def fail(*args):
                raise OSError("private database details")

            app.state.resources.store.snapshot = fail
            response = await client.get("/api/v1/overview")
            assert response.status_code == 503
            assert "private" not in response.text


async def test_schema_v1_migrates_without_dropping_history(stored):
    from pathlib import Path

    from psycopg.types.json import Jsonb

    _config, store, _source, _service = stored
    ts = int(time.time()) // 60 * 60
    async with store.pool.connection() as conn:
        await conn.execute(
            "DROP TABLE agent_task_memory,workload_resource_snapshot,host_metric_bucket,host_history_coverage,operations_decision,operations_record,operations_issue_event,operations_issue,node_metric_snapshot,investigation_feedback,investigation_event,investigation_task,resource_coarse_coverage,resource_history_coverage,resource_metric_bucket,resource_device,resource_model_binding,resource_business_model,operations_metric_snapshot,efficiency_review,efficiency_case,efficiency_investigation,efficiency_container_sample,efficiency_workload_sample,efficiency_device_sample,efficiency_coverage,history_rollup,history_fill_guard,history_source_state,history_sample,history_coverage,history_entity,history_progress,resource_snapshot,history_metadata CASCADE"
        )
        await conn.execute((Path(__file__).parents[1] / "app/history/schema.sql").read_text())
        await conn.execute("DROP TABLE resource_snapshot")
        await conn.execute("INSERT INTO history_metadata VALUES (1,1,%s)", (Jsonb(store.contract()),))
        await conn.execute(
            "INSERT INTO history_sample VALUES ('yz',60,%s,'cluster','{}',10,5,'healthy','[]')", (ts,)
        )
    await store.initialize()
    await store.initialize()
    rows = await store.read("yz", 60, ts, ts, 60)
    assert rows[0][1:3] == (10, 5)
    assert "旧历史口径待重新核验" in rows[0][4]
    async with store.pool.connection() as conn:
        assert (await (await conn.execute("SELECT version FROM history_metadata WHERE id=1")).fetchone()) == (
            15,
        )


async def test_invalid_persisted_payload_is_a_safe_unavailable(stored):
    _config, store, _source, service = stored
    await service.overview("yz", refresh=True)
    row = await store.snapshot("yz")
    row[0]["gpus"] = [{"device_uuid": "private-fixture-content"}]
    async with store.pool.connection() as c:
        await store.save_snapshot(c, "yz", row[0], row[1], time.time())
    from app.history.snapshots import SnapshotUnavailable

    with pytest.raises(SnapshotUnavailable) as exc:
        await service.overview("yz")
    assert "private-fixture-content" not in str(exc.value)


async def test_healthy_snapshot_does_not_duplicate_json_payload(stored):
    _config, store, _source, service = stored
    await service.overview("yz", refresh=True)
    row = await store.snapshot("yz")
    assert row[0]["status"] == "healthy"
    assert row[1] is None


async def test_read_attempt_success_and_commit_times_are_distinct(stored):
    _config, _store, source, service = stored
    first = await service.overview("yz", refresh=True)
    a = first.clusters[0]
    assert a.collection_succeeded_at == a.collection_attempted_at == a.fetched_at
    assert first.database_read_at == a.database_read_at
    assert a.database_read_at >= a.snapshot_stored_at >= a.collection_attempted_at

    async def fail(*args, **kwargs):
        from app.clients.prometheus import SourceError

        raise SourceError("fixture failure")

    source.instant_query = fail
    second = await service.overview("yz", refresh=True)
    b = second.clusters[0]
    assert b.status == "stale"
    assert b.collection_attempted_at > a.collection_attempted_at
    assert b.collection_succeeded_at == a.collection_succeeded_at
    assert b.database_read_at >= b.snapshot_stored_at


async def test_manual_refresh_is_cooled_down_across_service_instances(stored):
    config, store, source, service = stored
    config.snapshot_refresh_min_interval_seconds = 5
    first = await service.overview("yz", refresh=True)
    source.fixture["yz"][0]["allocations"][0]["value"] += 100
    other = SnapshotService(ResourceService(source, config), store)
    for _ in range(8):
        response = await other.overview("yz", refresh=True)
        assert response.clusters[0].collection_attempted_at == first.clusters[0].collection_attempted_at
        assert response.clusters[0].allocated_raw == first.clusters[0].allocated_raw
