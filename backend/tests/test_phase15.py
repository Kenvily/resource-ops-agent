import asyncio
import time

import httpx
import pytest

from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient, SourceError
from app.core.config import Settings
from app.core.telemetry import Telemetry
from app.core.time_ranges import four_months_ago
from app.main import create_app
from app.queries.hami import range_query_for
from app.services.cache import MemoryCache
from app.services.resources import ResourceService
from app.services.trends import TrendService, parse_matrix


def config(**kwargs):
    return Settings(_env_file=None, snapshot_refresh_min_interval_seconds=0, **kwargs)


def matrix(values):
    return {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}]},
    }


@pytest.mark.parametrize("window", ["1h", "24h", "7d", "4mo"])
async def test_windows_bounded_aligned_and_cached(window):
    settings = config()
    service = TrendService(ResourceService(MockPrometheusClient(settings), settings))
    data = await service.query("all", window)
    assert data.complete
    assert len(data.series) == 2
    assert data.start >= four_months_ago(data.end)
    assert all(len(s.points) <= settings.range_max_points for s in data.series)
    assert [p.timestamp for p in data.series[0].points] == [p.timestamp for p in data.series[1].points]
    point = data.series[0].points[0]
    assert point.capacity_raw == point.allocated_raw + point.unallocated_raw
    cached = await service.query("all", window)
    assert cached.cache_status == "hit"
    assert cached.fetched_at == data.fetched_at


@pytest.mark.parametrize(
    "scenario,status",
    [("partial", "unavailable"), ("missing", "degraded"), ("empty", "empty"), ("overallocated", "degraded")],
)
async def test_history_failures_and_gaps(scenario, status):
    settings = config(mock_scenario=scenario)
    data = await TrendService(ResourceService(MockPrometheusClient(settings), settings)).query("all", "1h")
    assert data.series[1].status == status
    assert data.complete is False
    if scenario == "partial":
        assert data.series[0].status == "healthy"
        assert all(p.allocated_raw is None for p in data.series[1].points)
    if scenario == "missing":
        assert data.series[0].points[3].allocated_raw is None
        assert data.series[0].points[3].unallocated_raw is None
    if scenario == "overallocated":
        assert data.series[0].points[0].unallocated_raw == -2000


def test_parser_never_zero_fills_nonfinite_or_misaligned_samples():
    parsed, warnings = parse_matrix(
        matrix([[0, "0"], [60, "NaN"], [120, "Inf"], [181, "3"], [240, "-1"]]), 0, 240, 60
    )
    assert parsed[0] == 0
    assert parsed[60] is None and parsed[120] is None and parsed[240] is None
    assert 180 not in parsed
    assert warnings


async def test_range_http_bounds_and_matrix_limits():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=matrix([[i, "1"] for i in range(1001)]))

    settings = config(data_mode="live", prom_yz_url="https://yz.test", prom_zw_url="https://zw.test")
    client = HttpPrometheusClient(settings, httpx.MockTransport(handler))
    for start, end, step in [(0, 124 * 86400, 86400), (0, 1000, 1), (100, 0, 60), (0, 60, 0)]:
        with pytest.raises(SourceError):
            await client.range_query("yz", "capacity", start, end, step)
    assert calls == []
    with pytest.raises(SourceError):
        await client.range_query("yz", "capacity", 0, 3600, 60)
    assert calls[0].url.path == "/api/v1/query_range"
    assert calls[0].url.params["start"] == "0"
    assert "sum(max by" in calls[0].url.params["query"]
    await client.aclose()


def test_fixed_range_templates_and_quoting():
    strict = range_query_for("yz", "allocated", False)
    zero = range_query_for("yz", "allocated", True)
    assert "count(" in strict and " or " not in strict
    assert " or " in zero
    quoted = range_query_for("yz", "capacity", False, ('x"} or up #', "uuid"))
    assert 'node="x\\"} or up #"' in quoted
    with pytest.raises(KeyError):
        range_query_for("bad", "capacity", True)


async def test_single_gpu_history_validates_inventory():
    settings = config()
    resources = ResourceService(MockPrometheusClient(settings), settings)
    trends = TrendService(resources)
    gpu = (await resources.overview("yz")).clusters[0].gpus[0]
    result = await trends.query("yz", "1h", (gpu.node, gpu.device_uuid))
    assert len(result.series) == 1
    assert result.series[0].points[0].capacity_raw == gpu.capacity_raw
    with pytest.raises(SourceError):
        await trends.query("yz", "1h", ("unknown", "unknown"))


async def test_snapshot_cache_coalescing_bypass_expiry_and_stale_failure():
    settings = config(prom_cache_ttl_seconds=15)

    class CountingMock(MockPrometheusClient):
        calls = 0
        failed = False

        async def instant_query(self, cluster, template, at):
            self.calls += 1
            if self.failed:
                raise SourceError("演示：超时")
            return await super().instant_query(cluster, template, at)

    client = CountingMock(settings)
    service = ResourceService(client, settings)
    first, second = await asyncio.gather(service.overview("yz"), service.overview("yz"))
    assert client.calls == 4
    assert {first.clusters[0].cache_status, second.clusters[0].cache_status} == {"miss", "hit"}
    await service.overview("yz", refresh=True)
    assert client.calls == 8
    client.failed = True
    stored = service.cache.entries[("yz",)][1]
    service.cache.entries[("yz",)] = (time.monotonic() - 20, stored)
    degraded = await service.overview("yz")
    assert degraded.clusters[0].status == "stale"
    assert degraded.clusters[0].cache_status == "stale"
    assert degraded.totals.gpu_count_observed is None
    assert degraded.clusters[0].gpu_count_observed == 12
    service.cache.entries[("yz",)] = (time.monotonic() - 601, stored)
    unavailable = await service.overview("yz")
    assert unavailable.clusters[0].status == "unavailable"


async def test_original_source_time_detects_stale_aggregate_evaluation():
    settings = config()

    class OldSource(MockPrometheusClient):
        async def instant_query(self, cluster, template, at):
            if template == "freshness":
                return {
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"metric": {}, "value": [at, "500"]}]},
                }
            return await super().instant_query(cluster, template, at)

    data = await ResourceService(OldSource(settings), settings).overview()
    assert all(c.status == "stale" for c in data.clusters)
    assert all(c.source_age_seconds == 500 for c in data.clusters)
    assert data.totals.gpu_count_observed is None


def test_cache_and_operational_stats_bounded_and_label_free():
    cache = MemoryCache(max_entries=2)
    for n in range(20):
        cache.put((n,), n)
    assert len(cache.entries) == 2 and len(cache.locks) == 16
    telemetry = Telemetry()
    for _ in range(600):
        telemetry.record("prometheus", "yz", "range/capacity", 0.1)
    telemetry.record("prometheus", "yz", "range/capacity", 0.2, "timeout")
    stat = telemetry.snapshot()["metrics"][0]
    assert stat["count"] == 601 and stat["recent_samples"] == 512
    assert stat["timeouts"] == 1 and stat["p95_ms"] == 100


async def test_history_api_safety_diagnostics_and_cache():
    settings = config()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.get("/api/v1/trends?window=1h")
        assert response.status_code == 200 and response.json()["complete"]
        assert (await client.get("/api/v1/trends?window=1h")).json()["cache_status"] == "hit"
        for query in [
            "window=30d",
            "window=1h&query=up",
            "window=1h&start=0",
            "window=1h&step=1",
            "node=x",
            "cluster=bad",
            "node=x&uuid=y",
        ]:
            assert (await client.get("/api/v1/trends?" + query)).status_code == 422
        assert (await client.get("/api/v1/trends?cluster=yz&node=x&uuid=y")).status_code == 404
        snapshot = await client.get("/api/v1/overview?refresh=true")
        assert snapshot.json()["clusters"][0]["cache_status"] == "miss"
        stats = (await client.get("/api/v1/diagnostics")).json()
        assert stats["metrics"]
        assert "demo-node" not in str(stats) and "GPU-DEMO" not in str(stats)
        assert (await client.get("/api/health", headers={"host": "evil.example"})).status_code == 400


@pytest.mark.parametrize(
    "end,expected",
    [
        ("2026-10-27T17:40:00+08:00", "2026-06-27T17:40:00+08:00"),
        ("2026-06-30T12:00:00+08:00", "2026-02-28T12:00:00+08:00"),
        ("2024-06-30T12:00:00+08:00", "2024-02-29T12:00:00+08:00"),
        ("2026-03-31T12:00:00+08:00", "2025-11-30T12:00:00+08:00"),
    ],
)
def test_calendar_four_month_boundary(end, expected):
    from datetime import datetime

    assert four_months_ago(int(datetime.fromisoformat(end).timestamp())) == int(
        datetime.fromisoformat(expected).timestamp()
    )


async def test_http_rejects_one_second_beyond_calendar_limit():
    from datetime import datetime

    end = int(datetime.fromisoformat("2026-10-27T17:40:00+08:00").timestamp())
    start = four_months_ago(end)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=matrix([]))

    settings = config(data_mode="live", prom_yz_url="https://yz.test", prom_zw_url="https://zw.test")
    client = HttpPrometheusClient(settings, httpx.MockTransport(handler))
    with pytest.raises(SourceError):
        await client.range_query("yz", "capacity", start - 1, end, 86400)
    assert calls == []
    await client.range_query("yz", "capacity", start, end, 86400)
    assert len(calls) == 1
    await client.aclose()


async def test_long_history_segments_recover_after_oldest_store_boundary(monkeypatch):
    from datetime import datetime

    anchor = int(datetime.fromisoformat("2026-10-27T17:40:00+08:00").timestamp())
    monkeypatch.setattr("app.services.trends.time.time", lambda: anchor)
    cutoff = anchor - 121 * 86400

    class PartialStore(MockPrometheusClient):
        calls = 0

        async def range_query(self, cluster, template, start, end, step, device=None):
            self.calls += 1
            assert end - start <= 30 * 86400
            if start < cutoff < end and end - start > 86400:
                return {
                    "status": "success",
                    "data": {"resultType": "matrix", "result": []},
                    "warnings": ["partial store"],
                }
            result = await super().range_query(cluster, template, start, end, step, device)
            for row in result["data"]["result"]:
                row["values"] = [point for point in row["values"] if point[0] >= cutoff]
            return result

    settings = config()
    source = PartialStore(settings)
    data = await TrendService(ResourceService(source, settings)).query("yz", "4mo")
    points = data.series[0].points
    assert points[0].allocated_raw is None
    assert points[-1].allocated_raw is not None
    assert sum(p.allocated_raw is not None for p in points) > 700
    assert data.series[0].status == "degraded"
    assert source.calls <= 64
