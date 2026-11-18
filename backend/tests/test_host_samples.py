# ruff: noqa: F811
import time

import pytest_asyncio
from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.operations.host_history import METRICS, HostHistoryService, HostRequest
from app.operations.host_samples import capture, fill_points, next_budget
from app.resources.trends import FleetTrendRequest, fleet_trends


@pytest_asyncio.fixture
async def clean(domain):
    store, *_ = domain
    async with store.pool.connection() as conn:
        await conn.execute("TRUNCATE host_history_point,host_point_coverage,host_metric_bucket")
    return domain


def test_backoff_recovers_after_success_instead_of_staying_one_hour():
    size, successes = next_budget(24, 24, True, 0)
    assert size == 12
    for _ in range(3):
        size, successes = next_budget(size, 24, False, successes)
    assert size == 15 and successes == 0


async def test_hourly_points_are_idempotent_not_fake_minute_statistics(clean):
    store, source, history, *_ = clean
    at = (int(time.time()) - 5 * 86400) // 14400 * 14400
    calls = []
    original = source.range_query

    async def record(cluster, key, start, end, step, *args):
        calls.append((key, step))
        return await original(cluster, key, start, end, step, *args)

    source.range_query = record
    assert await fill_points(history, "yz", start=at, end=at + 23 * 3600, budget=24) == 24
    assert all(step == 3600 for _, step in calls) and len(calls) == 8
    count = len(calls)
    assert await fill_points(history, "yz", start=at, end=at + 23 * 3600, budget=24) == 0
    assert len(calls) == count
    node = source.fixture["yz"][0]["node"]
    async with store.pool.connection() as conn:
        assert not (await (await conn.execute("SELECT count(*) FROM host_metric_bucket")).fetchone())[0]
        await capture(conn, "yz", at, [{"node": node}])
        value = (
            await (
                await conn.execute(
                    "SELECT value FROM host_history_point WHERE cluster='yz' AND node=%s AND metric='cpu_utilization_pct' AND ts=%s",
                    (node, at),
                )
            ).fetchone()
        )[0]
        assert value is not None  # a failed retry cannot overwrite a valid observation
    result = await HostHistoryService(history, source.settings).detail(
        HostRequest(cluster="yz", node=node, window="3mo", history_mode="auto")
    )
    assert result["history"]["sampling"] == "hourly_point"
    points = result["history"]["series"][0]["points"]
    observed = [p for p in points if p["sampled_value"] is not None]
    assert observed and all(p["mean"] is p["min"] is p["max"] is None for p in observed)
    assert all(p["valid_points"] == p["expected_points"] == 1 for p in observed)
    # Existing clients retain the statistics contract, never implicitly receive point samples.
    old = await HostHistoryService(history, source.settings).detail(
        HostRequest(cluster="yz", node=node, window="3mo")
    )
    assert old["history"]["sampling"] == "minute_statistics"


async def test_cpu_sample_fleet_is_weighted_and_database_only(clean):
    store, source, _history, _, service = clean
    at = (int(time.time()) - 5 * 86400) // 14400 * 14400
    async with store.pool.connection() as conn:
        for node, cpu, cores in (("fixture-a", 20, 2), ("fixture-b", 80, 6)):
            await capture(conn, "yz", at, [{"node": node, "cpu_utilization_pct": cpu, "cpu_cores": cores}])

    async def forbidden(*args, **kwargs):
        raise AssertionError("query cannot access Prometheus")

    source.range_query = forbidden
    result = await fleet_trends(
        service,
        FleetTrendRequest(cluster="yz", window="3mo", metric="cpu_utilization_pct", history_mode="auto"),
    )
    assert result["sampling"] == "hourly_point"
    assert next(p["value"] for p in result["series"][0]["points"] if p["timestamp"] == at) == 65
    assert not result["complete"]


async def test_transport_failure_shrinks_without_marking_whole_batch_attempted(clean):
    store, source, history, *_ = clean
    at = (int(time.time()) - 7 * 86400) // 3600 * 3600
    original = source.range_query

    async def fail_cpu(cluster, key, *args):
        if key == "host_cpu":
            raise TimeoutError()
        return await original(cluster, key, *args)

    source.range_query = fail_cpu
    await fill_points(history, "yz", start=at, end=at + 23 * 3600, budget=24)
    assert history.host_sample_budgets["yz"] == (12, 0)
    async with store.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM host_point_coverage")).fetchone())[0] == 0


async def test_additive_migration_keeps_statistics_and_repeats(clean):
    store, *_ = clean
    async with store.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO host_metric_bucket VALUES('yz','fixture-node','load1',3600,3600,120,1,3,60)"
        )
        await conn.execute("UPDATE history_metadata SET version=12")
    await store.initialize()
    await store.initialize()
    async with store.pool.connection() as conn:
        assert (
            await (
                await conn.execute("SELECT sum_value FROM host_metric_bucket WHERE node='fixture-node'")
            ).fetchone()
        )[0] == 120


async def test_equal_coverage_prefers_richer_statistics_and_empty_is_not_zero(clean):
    store, source, history, *_ = clean
    at = (int(time.time()) - 5 * 86400) // 14400 * 14400
    node = source.fixture["yz"][0]["node"]
    async with store.pool.connection() as conn:
        await capture(conn, "yz", at, [{"node": node, **{m: 10 for m in METRICS}}])
        for offset in range(0, 14400, 3600):
            for metric in METRICS:
                await conn.execute(
                    "INSERT INTO host_metric_bucket VALUES('yz',%s,%s,3600,%s,600,5,15,60)",
                    (node, metric, at + offset),
                )
    result = await HostHistoryService(history, source.settings).detail(
        HostRequest(cluster="yz", node=node, window="3mo", history_mode="auto")
    )
    assert result["history"]["sampling"] == "minute_statistics"
    points = result["history"]["series"][0]["points"]
    point = next(p for p in points if p["timestamp"] == at)
    assert point["mean"] == 10 and point["min"] == 5 and point["max"] == 15
    assert all(p["mean"] is None for p in points if p["timestamp"] != at)
