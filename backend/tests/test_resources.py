import asyncio
import time

import httpx
import pytest

from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient, SourceError
from app.core.config import Settings
from app.main import create_app
from app.queries.hami import query_for
from app.services.normalizer import normalize
from app.services.resources import ResourceService


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize(
    "scenario,status",
    [
        ("normal", "healthy"),
        ("partial", "degraded"),
        ("empty", "degraded"),
        ("missing", "degraded"),
        ("overallocated", "degraded"),
        ("stale", "degraded"),
    ],
)
async def test_scenarios(scenario, status):
    config = settings(mock_scenario=scenario)
    data = await ResourceService(MockPrometheusClient(config), config).overview()
    assert data.overall_status == status
    assert data.units.unit_verified is False
    if scenario == "normal":
        assert data.totals.gpu_count_observed == 20
        assert data.totals.capacity_raw == data.totals.allocated_raw + data.totals.unallocated_raw
        assert data.clusters[0].workloads[0].namespace == "model-serving"
        assert any(g.allocated_raw == 0 for g in data.clusters[0].gpus)
    elif scenario in ("missing", "overallocated"):
        assert data.totals.gpu_count_observed == 20
        assert data.totals.capacity_raw == 480000
    else:
        assert data.totals.gpu_count_observed is None
    if scenario == "partial":
        assert data.clusters[0].status == "healthy"
        assert data.clusters[1].status == "unavailable"
    if scenario == "overallocated":
        assert data.clusters[0].gpus[0].unallocated_raw == -2000
    if scenario == "missing":
        assert data.clusters[0].gpus[0].allocated_raw is None
        assert data.totals.allocated_raw is None
        assert data.totals.unallocated_raw is None
        assert data.totals.allocation_ratio is None


def vector(rows):
    return {"status": "success", "data": {"resultType": "vector", "result": rows}}


def row(value, timestamp, **labels):
    return {
        "metric": dict(cluster="k8s_yz_gpu_maas", node="demo-node", device_uuid="demo-gpu", **labels),
        "value": [timestamp, str(value)],
    }


def test_missing_is_not_zero_and_dedup():
    at = time.time()
    payloads = {
        "capacity": vector([row(100, at), row(100, at)]),
        "allocated": vector([]),
        "workloads": vector([]),
    }
    data = normalize(
        "yz",
        payloads,
        [],
        at,
        settings(data_mode="live", prom_yz_url="https://yz.test", prom_zw_url="https://zw.test"),
    )
    assert data.gpu_count_observed == 1
    assert data.allocated_raw is None
    assert data.status == "degraded"


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf", "-10"])
def test_nonfinite_and_negative(value):
    at = time.time()
    data = normalize("yz", {"capacity": vector([row(value, at)])}, [], at, settings())
    assert data.capacity_raw is None
    assert data.gpus[0].capacity_raw is None
    data.model_dump_json()  # never emits NaN as a metric


def test_orphan_and_misaligned():
    at = time.time()
    data = normalize("yz", {"allocated": vector([row(50, at)])}, [], at, settings())
    assert data.gpu_count_observed is None
    assert data.allocation_ratio is None
    data = normalize(
        "yz",
        {"capacity": vector([row(100, at)]), "allocated": vector([row(50, at - 10)])},
        [],
        at,
        settings(),
    )
    assert data.allocated_raw is None
    assert data.status == "degraded"


@pytest.mark.parametrize(
    "kind", ["timeout", "auth", "server", "json", "error", "large", "redirect", "series"]
)
async def test_http_errors(kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("secret internal URL")
        if kind == "auth":
            return httpx.Response(403)
        if kind == "server":
            return httpx.Response(500)
        if kind == "json":
            return httpx.Response(200, text="<html>internal secret</html>")
        if kind == "error":
            return httpx.Response(200, json={"status": "error", "error": "private label"})
        if kind == "large":
            return httpx.Response(200, content=b"x" * 2000)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test"})
        return httpx.Response(200, json=vector([{}, {}]))

    config = settings(
        data_mode="live",
        prom_yz_url="https://yz.test",
        prom_zw_url="https://zw.test",
        prom_max_response_bytes=1024,
        prom_max_series=1,
    )
    client = HttpPrometheusClient(config, httpx.MockTransport(handler))
    with pytest.raises(SourceError) as error:
        await client.instant_query("yz", "capacity", time.time())
    assert "secret" not in str(error.value) and "private" not in str(error.value)
    await client.aclose()


async def test_fixed_queries_same_instant():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=vector([]))

    config = settings(data_mode="live", prom_yz_url="https://yz.test", prom_zw_url="https://zw.test")
    client = HttpPrometheusClient(config, httpx.MockTransport(handler))
    await ResourceService(client, config).overview()
    assert len(seen) == 8
    assert {r.url.host for r in seen} == {"yz.test", "zw.test"}
    assert len({r.url.params["time"] for r in seen}) == 1
    assert all('cluster="k8s_' in r.url.params["query"] for r in seen)
    with pytest.raises(KeyError):
        query_for("evil", "capacity")
    await client.aclose()


async def test_wall_clock_timeout():
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=vector([]))

    config = settings(
        data_mode="live",
        prom_yz_url="https://yz.test",
        prom_zw_url="https://zw.test",
        prom_timeout_seconds=0.1,
    )
    client = HttpPrometheusClient(config, httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="超时"):
        await client.instant_query("yz", "capacity", time.time())
    await client.aclose()


async def test_api_drilldown_and_safety():
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/health")).json()["mode"] == "mock"
        data = (await client.get("/api/v1/overview")).json()
        gpu = data["clusters"][0]["gpus"][0]
        detail = await client.get(f"/api/v1/gpus/yz/{gpu['node']}/{gpu['device_uuid']}")
        assert detail.status_code == 200 and detail.json()["workloads"]
        assert (await client.get("/api/v1/nodes?cluster=zw")).json()["nodes"]
        assert (await client.get("/api/v1/workloads?level=pod")).json()["items"]
        assert (await client.get("/api/v1/gpus?cluster=yz")).json()["gpus"]
        assert (await client.get("/api/v1/clusters")).status_code == 200
        assert (await client.get("/api/v1/overview?cluster=bad")).status_code == 422
        assert (await client.get("/api/v1/query?query=up")).status_code == 422
        assert (await client.get("/api/v1/query")).status_code == 404
        assert (await client.get("/api/v1/overview?url=https://evil.test")).status_code == 422
        assert (await client.post("/api/v1/overview")).status_code == 405
        assert (await client.get("/api/v1/overview")).headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com",
        "file:///tmp/secret",
        "https://example.com/path",
        "https://example.com?query=up",
    ],
)
def test_config_rejects_unsafe_origins(url):
    with pytest.raises(ValueError):
        settings(data_mode="live", prom_yz_url=url, prom_zw_url="https://zw.test")


@pytest.mark.parametrize("metric", [None, [], {"node": 123}, {"cluster": "wrong"}])
def test_malformed_labels_are_degraded(metric):
    at = time.time()
    data = normalize(
        "yz", {"capacity": vector([{"metric": metric, "value": [at, "100"]}])}, [], at, settings()
    )
    assert data.status == "degraded"
    assert data.capacity_raw is None


def test_example_environment_loads_and_llm_is_disabled():
    from pathlib import Path

    config = Settings(_env_file=Path(__file__).resolve().parents[2] / ".env.example")
    assert config.data_mode == "mock"
    assert config.llm_enabled is False
    with pytest.raises(ValueError, match="authorization"):
        settings(llm_enabled=True)


@pytest.mark.parametrize("failed_template", ["allocated", "workloads"])
async def test_totals_preserve_independent_metrics(failed_template):
    config = settings()

    class IncompleteSource(MockPrometheusClient):
        async def instant_query(self, cluster, template, at):
            if template == failed_template:
                raise SourceError("演示：明细不可用")
            return await super().instant_query(cluster, template, at)

    data = await ResourceService(IncompleteSource(config), config).overview()
    assert data.complete is False
    assert data.overall_status == "degraded"
    assert data.totals.gpu_count_observed == 20
    assert data.totals.capacity_raw == 480000
    if failed_template == "allocated":
        assert data.totals.allocated_raw is None
        assert data.totals.allocation_ratio is None
    else:
        assert data.totals.allocated_raw == 222000
        assert data.totals.unallocated_raw == 258000


async def test_confirmed_absence_zero_and_memory_units():
    config = settings(
        mock_scenario="missing",
        allocation_absence_is_zero=True,
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
    )
    data = await ResourceService(MockPrometheusClient(config), config).overview()
    assert data.complete is True
    assert data.totals.allocated_raw is not None
    assert data.totals.unallocated_raw == data.totals.capacity_raw - data.totals.allocated_raw
    assert data.totals.allocation_observed_gpu_count == data.totals.gpu_count_observed
    assert data.units.unit_verified is True
    assert data.units.memory == "MB"
    assert data.clusters[0].gpus[0].allocated_raw == 0


async def test_observed_amounts_do_not_fabricate_missing_allocations():
    config = settings(mock_scenario="missing")
    data = await ResourceService(MockPrometheusClient(config), config).overview()
    assert data.totals.allocated_raw is None
    assert data.totals.allocated_observed_raw > 0
    assert data.totals.unallocated_observed_raw > 0
    assert data.totals.allocation_observed_gpu_count == 18
    assert data.units.allocation == "unverified"
