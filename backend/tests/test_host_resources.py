# ruff: noqa: F811
"""Host observations keep node identity, nulls, units and mergeable history intact."""

import time

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from test_analysis import context  # noqa: F401
from test_hosts import fixture
from test_resource_statistics import domain  # noqa: F401

from app.clients.prometheus import SourceError
from app.main import create_app
from app.operations.host_history import METRICS, HostHistoryService, HostRequest, fill_recent
from app.operations.hosts import host_query, normalize_hosts, read_hosts


def test_extended_disk_network_and_kubernetes_have_explicit_units_and_no_totals():
    inventory, info, payloads = fixture()
    row = normalize_hosts("yz", 120, inventory, info, payloads)[0]
    assert row["filesystems"][0]["total_bytes"] == 1e12
    assert row["filesystems"][0]["available_bytes"] == 6e11
    assert row["networks"][0]["receive_bytes_per_second"] == 2e6
    assert row["kubernetes"]["allocatable_cpu_cores"] == 60
    assert row["kubernetes"]["unschedulable"] is False
    assert row["kubernetes"]["pressure"]["MemoryPressure"] == "false"
    assert "filesystem_total_bytes" not in row and "network_total_bytes" not in row
    assert "rate(" in host_query("yz", "host_network_receive") and "[5m]" in host_query(
        "yz", "host_network_receive"
    )
    assert 'unit="byte"' in host_query("yz", "host_kube_allocatable_memory")


def test_conflicts_out_of_range_and_missing_are_not_converted_to_zero():
    inventory, info, p = fixture()
    p["host_filesystem_available"]["data"]["result"][0]["values"][0][1] = "2000000000000"
    p["host_network_receive"] = RuntimeError("private upstream error")
    p["host_kube_unschedulable"]["data"]["result"][0]["values"][0][1] = "2"
    p["host_load1"]["data"]["result"][0]["metric"]["cluster"] = "foreign"
    row = normalize_hosts("yz", 120, inventory, info, p)[0]
    assert row["filesystems"][0]["available_bytes"] is None
    assert row["networks"][0]["receive_bytes_per_second"] is None
    assert row["kubernetes"]["unschedulable"] is None
    assert row["load1"] is None
    assert "private" not in str(row)


@pytest_asyncio.fixture
async def hosts(domain):
    store, source, history, metrics, _ = domain
    source.settings.host_metrics_enabled = True
    async with store.pool.connection() as c:
        await c.execute("TRUNCATE host_metric_bucket,host_history_coverage,node_metric_snapshot")
    at = int(time.time()) // 3600 * 3600 - 3600
    service = HostHistoryService(history, source.settings)
    return service, store, source, history, metrics, at


def observations(source, at):
    node = source.fixture["yz"][0]["node"]
    row = {m: 10.0 for m in METRICS} | {
        "node": node,
        "cpu_utilization_pct": 20.0,
        "memory_total_bytes": 100.0,
        "memory_used_bytes": 40.0,
        "memory_available_bytes": 60.0,
    }
    return row


async def test_minute_dedup_weighted_rollup_missing_preservation_and_prune(hosts):
    service, store, source, _, _, at = hosts
    row = observations(source, at)
    async with store.pool.connection() as c:
        for i in range(60):
            point = row | {"cpu_utilization_pct": float(i)}
            await service.capture(c, "yz", at + i * 60, [point])
            await service.capture(c, "yz", at + i * 60, [point])
        await service.capture(c, "yz", at, [row | {"cpu_utilization_pct": None}])
        await service.rollup(c, "yz", at, at + 3540)
        sums = await (
            await c.execute(
                "SELECT sum_value,min_value,max_value,valid_points FROM host_metric_bucket WHERE cluster='yz' AND node=%s AND metric='cpu_utilization_pct' AND grain=3600 AND ts=%s",
                (row["node"], at),
            )
        ).fetchone()
    assert sums == (sum(range(60)), 0, 59, 60)
    await service.prune(at + 3 * 86400)
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM host_metric_bucket WHERE grain=60")).fetchone())[
            0
        ] == 0
        assert (
            await (
                await c.execute(
                    "SELECT valid_points FROM host_metric_bucket WHERE metric='cpu_utilization_pct' AND grain=3600 AND ts=%s",
                    (at,),
                )
            ).fetchone()
        )[0] == 60


async def test_history_is_database_only_gap_aware_bounded_and_scoped(hosts):
    service, store, source, _h, metrics, at = hosts
    row = observations(source, at)
    async with store.pool.connection() as c:
        await service.capture(c, "yz", at, [row])
        await service.rollup(c, "yz", at, at)
    await metrics.collect("yz")

    async def forbidden(*a, **kw):
        raise AssertionError("page read upstream")

    source.range_query = forbidden
    request = HostRequest(cluster="yz", node=row["node"], window="1d")
    report = await service.detail(request, at + 3600 + 120)
    cpu = next(s for s in report["history"]["series"] if s["metric"] == "cpu_utilization_pct")
    partial = next(p for p in cpu["points"] if p["valid_points"] == 1)
    assert partial["mean"] is None and partial["min"] == 20 and partial["coverage"] == 0.2
    for window in ("1h", "1d", "1w", "1mo", "3mo", "all"):
        request.window = window
        assert len((await service.detail(request, at + 3600 + 120))["history"]["series"][0]["points"]) <= 721
    with pytest.raises(SourceError):
        await service.detail(HostRequest(cluster="yz", node="not-present"))
    source.settings.insight_allowed_clusters = ["zw"]
    with pytest.raises(SourceError):
        await service.detail(request)


async def test_current_failures_stale_and_no_duplicate_devices(hosts):
    _service, store, _source, h, metrics, at = hosts
    now = int(time.time()) // 60 * 60
    await metrics.collect("yz", now)
    rows = await read_hosts(h, ["yz"])
    assert rows and all(r["networks"] and r["filesystems"] for r in rows)
    async with store.pool.connection() as c:
        await c.execute("UPDATE node_metric_snapshot SET evaluated_at=%s", (at - 3600,))
    old = await read_hosts(h, ["yz"])
    assert all(r["status"] == "stale" and r["networks"] == [] and r["kubernetes"] is None for r in old)


async def test_backfill_uses_historical_inventory_is_idempotent_and_has_budgets(hosts):
    _service, store, _source, h, _metrics, _at = hosts
    await fill_recent(h, "yz")
    async with store.pool.connection() as c:
        count = (await (await c.execute("SELECT count(*) FROM host_history_coverage")).fetchone())[0]
    assert count == 30
    await fill_recent(h, "yz")
    async with store.pool.connection() as c:
        count2 = (await (await c.execute("SELECT count(*) FROM host_history_coverage")).fetchone())[0]
    assert count2 == 60


async def test_schema_ten_upgrade_preserves_original_tables_and_repeats_safely(hosts):
    _service, store, _source, _h, _metrics, _at = hosts
    async with store.pool.connection() as c:
        before = (await (await c.execute("SELECT count(*) FROM resource_device")).fetchone())[0]
        await c.execute("DROP TABLE workload_resource_snapshot,host_metric_bucket,host_history_coverage")
        await c.execute("UPDATE history_metadata SET version=10 WHERE id=1")
    await store.initialize()
    await store.initialize()
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM resource_device")).fetchone())[0] == before


async def test_host_api_rejects_scope_extra_inputs_and_exposes_database_contract(hosts):
    service, _store, source, _h, metrics, _at = hosts
    await metrics.collect("yz")
    config = source.settings.model_copy(
        update={"history_database_url": SecretStr(""), "host_metrics_enabled": False}
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        app.state.host_history = service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            body = {"cluster": "yz", "node": source.fixture["yz"][0]["node"]}
            response = await client.post("/api/v1/resource-statistics/host", json=body)
            assert response.status_code == 200 and response.json()["current"]["networks"]
            assert (
                await client.post("/api/v1/resource-statistics/host", json=body | {"sql": "select 1"})
            ).status_code == 422
            assert (
                await client.post(
                    "/api/v1/resource-statistics/host",
                    json=body,
                    headers={"Origin": "https://foreign.example"},
                )
            ).status_code == 403
            source.settings.insight_allowed_clusters = ["zw"]
            assert (await client.post("/api/v1/resource-statistics/host", json=body)).status_code == 403


async def test_identity_query_failure_persists_cooldown_and_does_not_erase_facts(hosts):
    service, store, source, h, _metrics, _at = hosts
    point = (int(time.time()) - 120) // 60 * 60
    async with store.pool.connection() as c:
        await service.capture(c, "yz", point, [observations(source, point)])
        await service.rollup(c, "yz", point, point)
        # Keep the prior minute fact but force a controlled retry of the identity source.
        await c.execute(
            "UPDATE host_history_coverage SET complete=false,attempted_at=now()-interval '20 minutes'"
        )
    calls = 0

    async def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise SourceError("PRIVATE IDENTITY ERROR")

    source.range_query = broken
    await fill_recent(h, "yz", now=point + 120)
    status = await service.status(["yz"])
    assert status["clusters"][0]["error_minutes"] == 30
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM host_metric_bucket WHERE grain=60")).fetchone())[
            0
        ] == 6
    # Each failed block is recorded; same recent grid is not requested again within cooldown.
    await fill_recent(h, "yz", now=point + 120)
    async with store.pool.connection() as c:
        markers = await (
            await c.execute("SELECT count(*) FROM host_history_coverage WHERE attempt_status='error'")
        ).fetchone()
    assert markers[0] == 60 and calls == 4
    assert "PRIVATE" not in str(status)


def test_post_validation_status_and_kubernetes_units_remain_truthful():
    inventory, info, p = fixture()
    p["host_cpu_count"]["data"]["result"][0]["values"][0][1] = "32"
    p["host_memory_available"]["data"]["result"][0]["values"][0][1] = str(512 * 2**30)
    p["host_filesystem_available"]["data"]["result"][0]["values"][0][1] = str(2e12)
    p["host_kube_allocatable_cpu"]["data"]["result"][0]["metric"]["unit"] = "byte"
    row = normalize_hosts("yz", 120, inventory, info, p)[0]
    assert row["metric_statuses"]["host_cpu"] == "unverified"
    assert row["metric_statuses"]["host_memory_available"] == "unverified"
    assert row["extended_metric_statuses"]["host_filesystem_available"] == "unverified"
    assert row["kubernetes"]["allocatable_cpu_cores"] is None
    assert row["extended_metric_statuses"]["host_kube_allocatable_cpu"] == "unverified"


async def test_single_history_hole_does_not_requery_complete_neighbors(hosts):
    _service, store, source, h, _metrics, _at = hosts
    now = int(time.time())
    end = (now - 120) // 60 * 60
    await fill_recent(h, "yz", now=now)
    hole = end - 10 * 60
    async with store.pool.connection() as c:
        await c.execute("DELETE FROM host_history_coverage WHERE cluster='yz' AND ts=%s", (hole,))
    original = source.range_query
    calls = []

    async def scoped(cid, template, left, right, *args, **kwargs):
        calls.append((left, right))
        return await original(cid, template, left, right, *args, **kwargs)

    source.range_query = scoped
    await fill_recent(h, "yz", now=now)
    assert calls and all(left == right == hole for left, right in calls)
