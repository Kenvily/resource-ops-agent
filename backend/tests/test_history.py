import os
import time

import pytest
import pytest_asyncio
from psycopg.errors import CheckViolation

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.core.config import Settings
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.history.sync import HistorySync, bounds, materialize
from app.services.resources import ResourceService
from app.services.trends import TrendService


def settings(**kwargs):
    return Settings(
        _env_file=None,
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
        **kwargs,
    )


async def payloads(config, start, end, grain):
    source = MockPrometheusClient(config)
    return {
        name: await source.range_query("yz", template, start, end, grain)
        for name, template in (
            ("capacity", "device_capacity"),
            ("allocated", "device_allocated"),
            ("workloads", "workload_allocated"),
        )
    }


async def test_dimension_materialization_reconciles_all_levels():
    config = settings()
    start = 1800000000
    rows = materialize("yz", await payloads(config, start, start, 60), start, start, 60, config)
    by_dim = {}
    for row in rows:
        by_dim.setdefault(row[1], []).append(row)
    assert set(by_dim) == {"cluster", "node", "gpu", "namespace", "pod", "workload"}
    total = by_dim["cluster"][0]
    for dimension in ("node", "gpu", "namespace", "pod", "workload"):
        assert sum(row[4] for row in by_dim[dimension]) == total[4]
    assert sum(row[3] for row in by_dim["gpu"]) == total[3]
    assert all(row[3] is None for row in by_dim["pod"])
    assert all(row[5] == "healthy" for row in rows)
    assert all("exporter" not in row[2] for row in rows)


async def test_invalid_numbers_and_missing_allocation_are_not_zeroed():
    config = Settings(_env_file=None, allocation_absence_is_zero=False)
    start = 1800000000
    data = await payloads(config, start, start, 60)
    data["allocated"]["data"]["result"] = []
    rows = materialize("yz", data, start, start, 60, config)
    assert next(row for row in rows if row[1] == "cluster")[4] is None
    data["capacity"]["data"]["result"][0]["values"][0][1] = "NaN"
    rows = materialize("yz", data, start, start, 60, config)
    assert next(row for row in rows if row[1] == "cluster")[3] is None


async def test_reject_duplicate_or_off_grid_history_samples():
    config = settings()
    start = 1800000000
    data = await payloads(config, start, start, 60)
    row = data["capacity"]["data"]["result"][0]
    row["values"].append(row["values"][0])
    with pytest.raises(SourceError):
        materialize("yz", data, start, start, 60, config)


def test_retention_grids():
    now = 1800000000
    for grain in (60, 300, 3600):
        start, end = bounds(now, grain)
        assert start % grain == end % grain == 0
        assert end <= now - 120


@pytest_asyncio.fixture
async def database():
    url = os.getenv("TEST_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_HISTORY_DATABASE_URL to a dedicated disposable PostgreSQL database")
    config = settings(history_database_url=url)
    store = HistoryStore(config)
    await store.open()
    await store.initialize()
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE history_rollup, history_fill_guard, history_source_state, history_sample, history_progress, resource_snapshot, history_coverage, history_entity RESTART IDENTITY"
        )
    try:
        yield config, store
    finally:
        await store.close()


async def test_postgres_idempotent_dimensions_checkpoint_and_rollback(database):
    config, store = database
    source = MockPrometheusClient(config)
    sync = HistorySync(ResourceService(source, config), store)
    start = int(time.time()) // 60 * 60 - 600
    await sync.ingest("yz", 60, start, start + 120, True)
    first = await store.read("yz", 60, start, start + 120, 60)
    await sync.ingest("yz", 60, start, start + 120, True)
    assert await store.read("yz", 60, start, start + 120, 60) == first
    assert await store.progress("yz", 60) == start + 180
    async with store.pool.connection() as conn:
        rows = await (await conn.execute("SELECT count(*) FROM history_sample")).fetchone()
        assert rows[0] > len(first)
    with pytest.raises(CheckViolation):
        await store.write("yz", 60, start, start, [(start, "cluster", (), -1, 1, "healthy", [])], True)
    assert await store.read("yz", 60, start, start + 120, 60) == first
    assert await store.progress("yz", 60) == start + 180
    with pytest.raises(SourceError):
        await store.read("yz", 60, start, start, 60, "arbitrary")


@pytest.mark.parametrize("window", ["1h", "24h", "7d", "4mo"])
async def test_postgres_windows_use_storage_without_upstream_reads(database, window, monkeypatch):
    from types import SimpleNamespace

    frozen = int(time.time())
    monkeypatch.setattr("app.services.trends.time", SimpleNamespace(time=lambda: frozen))
    config, store = database
    source = MockPrometheusClient(config)
    resources = ResourceService(source, config)
    history = HistoryService(resources, store)
    service = TrendService(resources, history)
    first = await service.query("yz", window)
    grain = {"1h": 60, "24h": 300, "7d": 300, "4mo": 3600}[window]
    # Fill all requested points with deterministic exact-time samples.
    for ts in range(first.start, first.end + 1, first.step):
        await history.sync.ingest("yz", grain, ts, ts)

    async def forbidden(*args, **kwargs):
        raise AssertionError("stored history must not re-query Prometheus")

    source.range_query = forbidden
    response = await service.query("yz", window, refresh=True)
    assert response.complete
    assert len(response.series[0].points) <= 721
    assert all(p.unallocated_raw == p.capacity_raw - p.allocated_raw for p in response.series[0].points)


async def test_postgres_partial_warning_does_not_overwrite_or_advance(database):
    config, store = database
    source = MockPrometheusClient(config)
    sync = HistorySync(ResourceService(source, config), store)
    ts = int(time.time()) // 60 * 60 - 600
    await sync.ingest("yz", 60, ts, ts, True)
    original = source.range_query

    async def warning(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["warnings"] = ["partial"]
        return result

    source.range_query = warning
    with pytest.raises(SourceError):
        await sync.ingest("yz", 60, ts, ts + 60, True)
    assert await store.progress("yz", 60) == ts + 60
    assert len(await store.read("yz", 60, ts, ts + 60, 60)) == 1


async def test_postgres_contract_prevents_mixing_units_and_modes(database):
    config, _store = database
    other = HistoryStore(Settings(_env_file=None, history_database_url=config.history_database_url))
    await other.open()
    try:
        with pytest.raises(SourceError):
            await other.validate()
        with pytest.raises(SourceError):
            await other.initialize()
    finally:
        await other.close()


async def test_database_failure_never_bypasses_storage():
    config = settings()
    resources = ResourceService(MockPrometheusClient(config), config)

    class Unavailable:
        async def query(self, *args):
            raise OSError("private database details")

    response = await TrendService(resources, Unavailable()).query("yz", "1h")
    assert response.series[0].status == "unavailable"
    assert response.series[0].points[0].capacity_raw is None
    assert "private" not in str(response)


async def test_incremental_resume_failure_isolation_and_repair(database):
    config, store = database
    source = MockPrometheusClient(config)
    sync = HistorySync(ResourceService(source, config), store)
    now = int(time.time())
    await sync.tick(budget=1, now=now)
    previous = await store.progress("zw", 60)
    assert previous is not None
    assert len(await store.status()) == 6
    original = source.range_query

    async def failed_yz(cluster, *args, **kwargs):
        if cluster == "yz":
            raise SourceError("fixture failure")
        return await original(cluster, *args, **kwargs)

    source.range_query = failed_yz
    await sync.tick(budget=1, now=now)
    assert await store.progress("zw", 60) > previous
    assert sync.last_status == "partial"
    assert sync.scope_status["yz/60"] == "source_error"
    await store.reset_progress()
    assert await store.status() == []
    assert await store.read("zw", 60, previous - 60, previous - 60, 60)


async def test_historical_entities_and_namespace_have_no_invented_capacity(database):
    config, store = database
    history = HistoryService(ResourceService(MockPrometheusClient(config), config), store)
    ts = int(time.time()) // 300 * 300 - 600
    await history.sync.ingest("yz", 300, ts, ts)
    found = await history.entities("yz", 300, ts, ts, "namespace")
    assert found["entities"] and not found["truncated"]
    response = await history.query("yz", 300, ts, ts, 300, "namespace", found["entities"][0])
    assert response.status == "healthy"
    assert response.points[0].capacity_raw is None
    assert response.points[0].allocation_ratio is None
    gpu = (await history.entities("yz", 300, ts, ts, "gpu"))["entities"][0]
    assert (await history.query("yz", 300, ts, ts, 300, "gpu", gpu)).status == "healthy"


async def test_retention_prunes_only_expired_tiers(database):
    _config, store = database
    now = int(time.time()) // 3600 * 3600
    old = now - 3 * 86400
    await store.write("yz", 60, old, old, [(old, "cluster", (), 10, 5, "healthy", [])])
    await store.write("yz", 3600, old, old, [(old, "cluster", (), 10, 5, "healthy", [])])
    await store.prune(now)
    assert not await store.read("yz", 60, old, old, 60)
    assert await store.read("yz", 3600, old, old, 3600)


async def test_orphan_allocations_never_become_complete_history():
    config = settings()
    ts = 1800000000
    data = await payloads(config, ts, ts, 60)
    expected_capacity = sum(float(r["values"][0][1]) for r in data["capacity"]["data"]["result"])
    data["allocated"]["data"]["result"].append(
        {
            "metric": {"cluster": "k8s_yz_gpu_maas", "node": "retired-node", "device_uuid": "orphan"},
            "values": [[ts, "10"]],
        }
    )
    row = next(r for r in materialize("yz", data, ts, ts, 60, config) if r[1] == "cluster")
    assert row[3] == expected_capacity
    assert row[4] is None
    assert row[5] == "degraded"


async def test_sync_advisory_lock_released_after_cancellation(database):
    import asyncio

    config, store = database
    sync = HistorySync(ResourceService(MockPrometheusClient(config), config), store)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await hold.wait()

    sync.ingest = blocked
    task = asyncio.create_task(sync.tick(budget=0))
    await asyncio.wait_for(entered.wait(), 2)
    async with store.pool.connection() as conn:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(7199)")).fetchone()
        assert row == (False,)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store.pool.connection() as conn:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(7199)")).fetchone()
        assert row == (True,)
        await conn.execute("SELECT pg_advisory_unlock(7199)")


async def test_malformed_source_response_is_a_safe_sync_failure():
    config = settings()
    source = MockPrometheusClient(config)
    original = source.range_query

    async def malformed(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["data"]["result"][0]["values"][0] = [None, "private-content"]
        return result

    source.range_query = malformed

    class NoWrites:
        async def write(self, *args, **kwargs):
            raise AssertionError("invalid response must not reach the database")

    sync = HistorySync(ResourceService(source, config), NoWrites())
    ts = int(time.time()) // 60 * 60 - 600
    with pytest.raises(SourceError, match="响应格式或样本异常"):
        await sync.ingest("yz", 60, ts, ts)


async def test_recent_priority_fills_hour_without_advancing_old_checkpoint(database):
    config, store = database
    service = ResourceService(MockPrometheusClient(config), config)
    sync = HistorySync(service, store)
    from app.history.sync import bounds

    now = int(time.time())
    first, end = bounds(now, 60)
    await sync.ingest("yz", 60, first, first, True)
    progress = await store.progress("yz", 60)
    await sync.ingest("yz", 60, end - 60, end)
    await sync.refresh_recent("yz", 60, first, end)
    rows = await store.read("yz", 60, end - 3600, end, 60)
    assert len(rows) == 61 and all(r[3] == "healthy" for r in rows)
    assert await store.progress("yz", 60) == progress
    from app.history.service import HistoryService

    result = await HistoryService(service, store).query("yz", 60, end - 3600, end, 60)
    assert result.expected_points == 61 and result.observed_points == 61
    assert result.missing_points == 0 and result.invalid_points == 0
    assert result.history_collected_at is not None and result.database_read_at >= result.history_collected_at


async def test_rollups_survive_prune_with_correct_sparse_statistics(database):
    config, store = database
    from app.history.service import HistoryService

    now = int(time.time())
    start = (now - 3 * 86400) // 3600 * 3600
    rows = [
        (start + i * 60, "cluster", (), 100, float(i), "degraded" if i == 10 else "healthy", [])
        for i in range(60)
    ]
    await store.write("yz", 60, start, start + 3540, rows, source_ages={r[0]: 0 for r in rows})
    history = HistoryService(ResourceService(MockPrometheusClient(config), config), store)
    before = await history.statistics("yz", 60, start, start + 3600)
    bucket = before["buckets"][0]
    metric = bucket["metrics"]["allocated"]
    assert bucket["valid_count"] == 59 and bucket["expected_count"] == 60
    assert metric["mean"] == pytest.approx((sum(range(60)) - 10) / 59)
    assert metric["min"] == 0 and metric["max"] == 59 and metric["coverage"] == pytest.approx(59 / 60)
    await store.prune(now)
    assert await store.read("yz", 60, start, start + 3540, 60) == []
    after = await history.statistics("yz", 60, start, start + 3600)
    assert after["buckets"][0]["metrics"] == bucket["metrics"]
    assert after["buckets"][0]["archived"]
    await store.prune(now)
    assert (await history.statistics("yz", 60, start, start + 3600)) == after


async def test_prune_rollup_failure_does_not_delete_samples(database, monkeypatch):
    _config, store = database
    start = (int(time.time()) - 3 * 86400) // 3600 * 3600
    rows = [(start, "cluster", (), 100, 50, "healthy", [])]
    await store.write("yz", 60, start, start, rows, source_ages={start: 0})

    async def failed(*args, **kwargs):
        raise RuntimeError("fixture aggregation failure")

    monkeypatch.setattr("app.history.store.aggregate", failed)
    with pytest.raises(RuntimeError):
        await store.prune(int(time.time()))
    assert len(await store.read("yz", 60, start, start, 60)) == 1


async def test_tail_schedule_respects_each_grain(database):
    config, store = database
    sync = HistorySync(ResourceService(MockPrometheusClient(config), config), store)
    calls = []

    async def record(cluster, grain, *args, **kwargs):
        calls.append((cluster, grain))

    async def skip(*args, **kwargs):
        pass

    sync.ingest = record
    sync.refresh_recent = skip
    store.prune = skip
    now = int(time.time()) // 3600 * 3600 + 130
    await sync.tick(budget=0, now=now)
    assert len(calls) == 6
    calls.clear()
    await sync.tick(budget=0, now=now + 60)
    assert calls == [("yz", 60), ("zw", 60)]
    calls.clear()
    await sync.tick(budget=0, now=now + 300)
    assert set(calls) == {("yz", 60), ("yz", 300), ("zw", 60), ("zw", 300)}


async def test_source_start_discovery_finds_short_lived_older_samples(database):
    config, store = database
    config.data_mode = "live"
    earliest = int(time.time()) - 210 * 86400 + 17

    class Source:
        calls = 0

        async def range_query(self, cluster, template, start, end, step):
            assert template == "first_seen"
            self.calls += 1
            points = [[t, "1"] for t in range(start, end + 1, step) if earliest <= t <= earliest + 30 * 86400]
            return {"data": {"result": [{"values": points}]}} if points else {"data": {"result": []}}

    source = Source()
    sync = HistorySync(ResourceService(source, config), store)
    found = await sync.discover_start("yz")
    assert earliest <= found <= earliest + 60
    calls = source.calls
    assert await sync.discover_start("yz") == found
    assert source.calls == calls and calls <= 20


async def test_history_fill_cooldown_is_shared_by_stores(database):
    config, store = database
    from app.history.store import HistoryStore

    other = HistoryStore(config)
    await other.open()
    try:
        assert await store.claim_fill("yz", 60, 30)
        assert not await other.claim_fill("yz", 60, 30)
        assert await other.claim_fill("zw", 60, 30)
    finally:
        await other.close()


async def test_partial_discovery_does_not_hide_older_available_points(database):
    config, store = database
    config.data_mode = "live"
    now = int(time.time())
    old = now - 180 * 86400 + 17
    recent = now - 30 * 86400

    class Source:
        async def range_query(self, cluster, template, start, end, step):
            partial = template == "first_seen" or end - start > 90 * 86400
            earliest = recent if partial else old
            points = [[t, "1"] for t in range(start, end + 1, step) if earliest <= t]
            result = {"data": {"result": [{"values": points}] if points else []}}
            if partial:
                result["warnings"] = ["fixture partial store response"]
            return result

    sync = HistorySync(ResourceService(Source(), config), store)
    discovered = await sync.discover_start("yz")
    assert old <= discovered <= old + 60
    async with store.pool.connection() as conn:
        assert await (
            await conn.execute("SELECT discovery_complete FROM history_source_state WHERE cluster='yz'")
        ).fetchone() == (False,)


async def test_daily_rollup_aligns_to_shanghai_midnight(database):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.history.service import HistoryService

    config, store = database
    start = int(
        datetime.fromtimestamp(time.time() - 3 * 86400, ZoneInfo("Asia/Shanghai"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    rows = [(start + i * 3600, "cluster", (), 100, float(i), "healthy", []) for i in range(24)]
    await store.write("yz", 3600, start, start + 23 * 3600, rows, source_ages={r[0]: 0 for r in rows})
    report = await HistoryService(ResourceService(MockPrometheusClient(config), config), store).statistics(
        "yz", 3600, start, start + 86400
    )
    assert report["buckets"][0]["start"] == start
    assert report["summary"]["allocated"]["mean"] == 11.5
    assert report["summary"]["allocated"]["valid_count"] == 24
    assert report["summary"]["allocated"]["coverage"] == 1


async def test_all_window_uses_discovered_start_beyond_four_months(database):
    config, store = database
    from app.history.service import HistoryService
    from app.services.trends import TrendService

    first = int(time.time()) - 210 * 86400
    await store.save_earliest("yz", first)
    history = HistoryService(ResourceService(MockPrometheusClient(config), config), store)
    response = await TrendService(history.resources, history).query("yz", "all")
    assert response.start >= first and response.start - first < response.step
    assert response.end - response.start > 200 * 86400
    assert len(response.series[0].points) <= 721


async def test_existing_samples_get_statistics_without_upstream(database):
    config, store = database
    start = (int(time.time()) - 7200) // 3600 * 3600
    await store.write(
        "yz", 60, start, start, [(start, "cluster", (), 100, 42, "healthy", [])], source_ages={start: 0}
    )
    async with store.pool.connection() as conn:
        await conn.execute("DELETE FROM history_rollup")
    await store.refresh_rollups(int(time.time()))
    from app.history.service import HistoryService

    report = await HistoryService(ResourceService(MockPrometheusClient(config), config), store).statistics(
        "yz", 60, start, start + 3600
    )
    assert report["summary"]["allocated"]["mean"] == 42
    assert report["summary"]["allocated"]["valid_count"] == 1
    assert report["summary"]["allocated"]["coverage"] == pytest.approx(1 / 60)


async def test_statistics_http_grain_is_accepted_and_typed(database):
    import httpx

    from app.main import create_app

    config, store = database
    start = (int(time.time()) - 7200) // 3600 * 3600
    await store.write(
        "yz", 60, start, start, [(start, "cluster", (), 100, 42, "healthy", [])], source_ages={start: 0}
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/history/statistics?cluster=yz&window=1d&grain=60")
        assert response.status_code == 200
        assert response.json()["summary"]["allocated"]["mean"] == 42
        assert (
            await client.get("/api/v1/history/statistics?cluster=yz&window=1d&grain=10")
        ).status_code == 422


async def test_all_coverage_starts_at_each_clusters_discovered_birth(database):
    config, store = database
    from app.history.service import HistoryService
    from app.services.trends import TrendService

    now = int(time.time())
    end = (now - 120) // 3600 * 3600
    service = HistoryService(ResourceService(MockPrometheusClient(config), config), store)
    for cluster, hours in [("yz", 12), ("zw", 24)]:
        start = end - hours * 3600
        await store.save_earliest(cluster, start)
        await service.sync.ingest(cluster, 3600, start, end)
    response = await TrendService(service.resources, service).query("all", "all")
    assert response.complete
    yz = response.series[0]
    assert yz.source_start_at == end - 12 * 3600 and yz.expected_points == 13
    assert len(yz.points) == 25 and all(p.capacity_raw is None for p in yz.points[:12])
    assert yz.observed_points == 13 and yz.missing_points == 0


async def test_summary_weights_valid_counts_instead_of_bucket_means(database):
    from app.history.service import HistoryService

    config, store = database
    start = (int(time.time()) - 14400) // 3600 * 3600
    await store.write(
        "yz", 60, start, start, [(start, "cluster", (), 100, 100, "healthy", [])], source_ages={start: 0}
    )
    rows = [
        (start + 3600 + i * 60, "cluster", (), 100, float(value), "healthy", [])
        for i, value in enumerate([0, 30, 60])
    ]
    await store.write("yz", 60, start + 3600, start + 3720, rows, source_ages={r[0]: 0 for r in rows})
    report = await HistoryService(ResourceService(MockPrometheusClient(config), config), store).statistics(
        "yz", 60, start, start + 7200
    )
    metric = report["summary"]["allocated"]
    assert metric["mean"] == 47.5 and metric["valid_count"] == 4
    assert metric["coverage"] == pytest.approx(4 / 120)


async def test_partial_discovery_checks_earlier_segments_across_a_gap(database):
    config, store = database
    config.data_mode = "live"
    now = int(time.time())
    old = (now - 180 * 86400) // 3600 * 3600 + 61
    late = old + 2 * 86400

    class Source:
        async def range_query(self, cluster, template, start, end, step):
            values = []
            for ts in range(start, end + 1, step):
                if ts >= late or (start == end and old <= ts <= old + 3 * 3600):
                    values.append([ts, "1"])
            return {
                "data": {"result": [{"values": values}] if values else []},
                "warnings": ["fixture partial stores"] if template == "first_seen" else [],
            }

    sync = HistorySync(ResourceService(Source(), config), store)
    earliest = await sync.discover_start("yz")
    assert old <= earliest <= old + 3600


async def test_automatic_hour_statistics_use_exact_range_samples(database, monkeypatch):
    import httpx

    from app.main import create_app

    config, store = database
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    end = (now - 120) // 60 * 60
    start = end - 3600
    rows = [(start + i * 60, "cluster", (), 100, float(i), "healthy", []) for i in range(61)]
    await store.write("yz", 60, start, end, rows, source_ages={r[0]: 0 for r in rows})
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/history/statistics?cluster=yz&window=1h")
        assert response.status_code == 200
        report = response.json()
        assert report["source_grain"] == 60 and report["statistics_source"] == "samples"
        assert report["start"] == start and report["end"] == end
        assert report["summary"]["allocated"]["mean"] == 30
        assert report["summary"]["allocated"]["min"] == 0 and report["summary"]["allocated"]["max"] == 60
        assert report["summary"]["allocated"]["valid_count"] == 61
        assert report["summary"]["allocated"]["coverage"] == 1


@pytest.mark.parametrize(
    "window,grain", [("1d", 300), ("1w", 300), ("1mo", 3600), ("3mo", 3600), ("all", 3600)]
)
async def test_automatic_statistics_select_grain_from_window(database, window, grain):
    import httpx

    from app.main import create_app

    config, store = database
    ts = (int(time.time()) - 600) // grain * grain
    await store.write("yz", grain, ts, ts, [(ts, "cluster", (), 100, 40, "healthy", [])], source_ages={ts: 0})
    await store.save_earliest("yz", ts - 180 * 86400)
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        report = (await client.get(f"/api/v1/history/statistics?cluster=yz&window={window}")).json()
        assert report["source_grain"] == grain
        assert report["summary"]["allocated"]["mean"] == 40
        assert report["statistics_source"] == "samples"


async def test_automatic_statistics_fall_back_to_archive_without_hiding_bad_samples(database):
    config, store = database
    from app.history.service import HistoryService

    start = (int(time.time()) - 3 * 86400) // 3600 * 3600
    row = (start, "cluster", (), 100, 40, "healthy", [])
    await store.write("yz", 60, start, start, [row], source_ages={start: 0})
    await store.prune(int(time.time()))
    history = HistoryService(ResourceService(MockPrometheusClient(config), config), store)
    archived = await history.window_statistics("yz", 300, start, start + 3600)
    assert archived["statistics_source"] == "archive" and archived["source_grain"] == 60
    assert archived["summary"]["allocated"]["mean"] == 40 and archived["warnings"]
    bad = (start, "cluster", (), 100, 50, "degraded", [])
    await store.write("yz", 60, start, start, [bad], source_ages={start: 0})
    report = await history.window_statistics("yz", 60, start, start + 3600)
    assert report["statistics_source"] == "samples"
    assert report["summary"]["allocated"]["valid_count"] == 0
    assert report["summary"]["allocated"]["mean"] is None
