import time

# ruff: noqa: F811
from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.operations.host_coarse import fill_hours, indexed_points


def test_minute_index_preserves_quality_metadata():
    p = {
        "status": "success",
        "warnings": ["partial"],
        "data": {"result": [{"metric": {}, "values": [[60, "1"], [120, "2"]]}]},
    }
    assert indexed_points(p)[60]["warnings"] == ["partial"]
    assert indexed_points(p)[120]["data"]["result"][0]["values"] == [[120, "2"]]


async def test_host_hour_backfill_stores_sufficient_statistics_idempotently(domain):
    store, _source, history, *_ = domain
    end = (int(time.time()) - 3 * 86400) // 3600 * 3600
    await fill_hours(history, "yz", start=end, end=end, budget=1)
    async with store.pool.connection() as c:
        rows = await (
            await c.execute(
                "SELECT metric,min(valid_points),max(valid_points) FROM host_metric_bucket WHERE cluster='yz' AND grain=3600 AND ts=%s GROUP BY metric",
                (end,),
            )
        ).fetchall()
    assert len(rows) == 6
    assert all(r[1] == r[2] == 60 for r in rows), rows
    assert await fill_hours(history, "yz", start=end, end=end, budget=1) == 0


async def test_fleet_history_cpu_is_core_weighted_database_only_and_missing_stays_unknown(domain):
    from app.resources.trends import FleetTrendRequest, fleet_trends

    store, source, _history, _metrics, service = domain
    end = (int(time.time()) - 120) // 60 * 60
    async with store.pool.connection() as c:
        await c.execute(
            "DELETE FROM host_metric_bucket WHERE cluster='yz' AND grain=60 AND ts>=%s", (end - 3600,)
        )
        for node, cpu, cores in (("fixture-a", 20, 2), ("fixture-b", 80, 6)):
            for metric, value in (("cpu_utilization_pct", cpu), ("cpu_cores", cores)):
                await c.execute(
                    "INSERT INTO host_metric_bucket VALUES('yz',%s,%s,60,%s,%s,%s,%s,1)",
                    (node, metric, end - 60, value, value, value),
                )

    async def forbidden(*args, **kwargs):
        raise AssertionError("history reads cannot access Prometheus")

    source.range_query = forbidden
    result = await fleet_trends(
        service, FleetTrendRequest(cluster="yz", window="1h", metric="cpu_utilization_pct")
    )
    assert len(result["series"][0]["points"]) <= 721
    values = [p["value"] for p in result["series"][0]["points"]]
    assert values[-1] == 65 and values[0] is None
    assert not result["complete"]


async def test_server_detail_reads_old_hourly_buckets_beyond_one_day(domain):
    from app.operations.host_history import HostHistoryService, HostRequest

    _store, source, history, *_ = domain
    end = int(time.time()) // 14400 * 14400 - 3 * 86400 - 3600
    await fill_hours(history, "yz", start=end - 3 * 3600, end=end, budget=4)
    node = source.fixture["yz"][0]["node"]
    result = await HostHistoryService(history, source.settings).detail(
        HostRequest(cluster="yz", node=node, window="3mo")
    )
    cpu = next(s for s in result["history"]["series"] if s["metric"] == "cpu_utilization_pct")
    assert any(p["timestamp"] < time.time() - 86400 and p["mean"] is not None for p in cpu["points"])
