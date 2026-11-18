"""Resource ledgers, strict ownership and mergeable history correctness."""

# ruff: noqa: F811
import time

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError
from test_analysis import context  # noqa: F401

from app.clients.prometheus import SourceError
from app.core.config import Settings
from app.history.snapshots import SnapshotService
from app.main import create_app
from app.operations.metrics import MetricsService
from app.resources.backfill import fill_recent
from app.resources.coarse import combine_hour, fill_hours
from app.resources.models import DeviceHistoryRequest, ModelBinding, StatisticsRequest
from app.resources.queries import resource_query
from app.resources.service import ResourceStatisticsService, aggregate, unmapped_workloads


@pytest_asyncio.fixture
async def domain(context):
    store, source, history, *_ = context
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE resource_coarse_coverage,resource_history_coverage,resource_metric_bucket,resource_device,resource_model_binding,resource_business_model,operations_metric_snapshot CASCADE"
        )
    snapshots = SnapshotService(history.resources, store)
    for cid in ("yz", "zw"):
        await snapshots.synchronize(cid, True)
    metrics = MetricsService(history, source.settings)
    for cid in ("yz", "zw"):
        await metrics.collect(cid)
    yield store, source, history, metrics, ResourceStatisticsService(history, source.settings)


def test_aggregate_keeps_missing_and_separates_memory_ledgers():
    d = {
        "cluster": "yz",
        "node": "machine",
        "hami_capacity_raw": 48000,
        "allocated_raw": 20000,
        "unallocated_raw": 28000,
        "physical_used_mib": 1024,
        "physical_free_mib": 22000,
        "utilization_pct": 10,
    }
    result = aggregate([d, {**d, "physical_used_mib": None, "utilization_pct": None}])
    assert result["allocated_raw"] == 40000 and result["gpu_count"] == 2
    assert result["machine_count"] == 1
    assert result["physical_used_mib"] is None and result["physical_usage_ratio"] is None
    assert result["utilization_pct"] is None and result["usage_observed_gpus"] == 1


def test_hour_subqueries_use_60_minute_evaluations_and_bounded_metrics():
    for stat in ("sum", "min", "max", "count"):
        expr = resource_query("yz", "res_gpu_memory_" + stat)
        assert "[59m:1m]" in expr and 'cluster="k8s_yz_gpu_maas"' in expr and "<=120" in expr
    with pytest.raises((KeyError, ValueError)):
        resource_query("yz", "res_arbitrary_sum")
    assert combine_hour([120, 1, 3, 60]) == (120, 1, 3, 60)
    assert combine_hour([120, 1, 3, 61]) == (None, None, None, 0)
    assert combine_hour([120, 4, 5, 60]) == (None, None, None, 0)


def test_model_mapping_validation_requires_explicit_unambiguous_identity():
    binding = {
        "cluster": "yz",
        "namespace_name": "demo",
        "pod_name": "pod",
        "container_name": "worker",
        "model_key": "model-a",
        "display_name": "演示模型",
    }
    with pytest.raises(ValidationError):
        Settings(_env_file=None, resource_model_mappings=[binding, binding])
    with pytest.raises(ValidationError):
        ModelBinding(**{**binding, "cluster": "all"})
    with pytest.raises(ValidationError):
        StatisticsRequest(window="1d")


async def test_current_statistics_are_database_only_and_model_unknown_is_explicit(domain):
    _store, source, _history, _metrics, service = domain

    async def forbidden(*args, **kwargs):
        raise AssertionError("statistics called Prometheus")

    source.range_query = forbidden
    source.instant_query = forbidden
    result = await service.statistics(StatisticsRequest())
    observed = sum(len(source.fixture[c]) for c in ("yz", "zw"))
    assert result["summary"]["gpu_count"] == observed
    values = [2 if i % 4 == 0 else 65 for cid in ("yz", "zw") for i, _ in enumerate(source.fixture[cid])]
    assert result["summary"]["utilization_pct"] == sum(values) / observed
    used = [
        (128e6 if i % 4 == 0 else 8e9) / 2**20
        for cid in ("yz", "zw")
        for i, _ in enumerate(source.fixture[cid])
    ]
    assert result["summary"]["physical_used_mib"] == sum(used)
    assert result["summary"]["allocated_raw"] == sum(m["allocated_raw"] for m in result["business_models"])
    assert all(m["name"] == "归属未知" and m["model_key"] is None for m in result["business_models"])
    assert result["unmapped_workload_count"] > 1
    assert sum(w["allocated_raw"] for w in result["unmapped_workloads"]) == result["summary"]["allocated_raw"]
    assert all(w["model_key"] is None and w["pod"] for w in result["unmapped_workloads"])
    assert sum(m["gpu_count"] for m in result["gpu_models"]) == observed
    assert all(d["gpu_model"] == "MOCK-GPU" for d in result["devices"])
    assert sum(m["gpu_count"] for m in result["machines"]) == observed


def test_unmapped_workloads_keep_cluster_namespace_and_pod_without_inference():
    from app.models.resource import WorkloadAllocation

    base = {
        "cluster": "yz",
        "node": "node",
        "device_uuid": "gpu-1",
        "namespace": "team-a",
        "pod": "model-like-name",
        "container": "worker",
        "allocated_raw": 100,
        "sampled_at": 1,
    }

    rows = [
        WorkloadAllocation(**values)
        for values in (
            base,
            {**base, "device_uuid": "gpu-2"},
            {**base, "namespace": "team-b"},
            {**base, "cluster": "zw"},
            {**base, "pod": "mapped-pod"},
            {**base, "pod": "missing-value", "allocated_raw": None},
        )
    ]
    mapping = {("yz", "team-a", "mapped-pod", "worker"): ("known", "已确认模型")}
    groups, count = unmapped_workloads(rows, mapping)
    assert count == 4
    shared = next(
        w
        for w in groups
        if w["cluster"] == "yz" and w["namespace"] == "team-a" and w["pod"] == "model-like-name"
    )
    assert shared["allocated_raw"] == 200 and shared["gpu_count"] == 2 and shared["container_count"] == 1
    assert all(w["model_key"] is None for w in groups)
    assert next(w for w in groups if w["pod"] == "missing-value")["allocated_raw"] is None
    assert unmapped_workloads(rows, mapping, limit=1)[1] == 4


async def test_shared_device_is_not_double_counted_as_physical_inventory(domain):
    store, source, history, _metrics, service = domain
    # Explicitly add two business owners to the same fixture GPU.
    gpu = source.fixture["yz"][0]
    first = gpu["allocations"][0]
    second = {**first, "namespace": "second-team", "pod": "second-pod", "container": "worker", "value": 1000}
    gpu["allocations"].append(second)
    snapshots = SnapshotService(history.resources, store)
    source.settings.snapshot_refresh_min_interval_seconds = 0
    await snapshots.synchronize("yz", True, requested_at=time.time() + 1)
    mappings = [
        ModelBinding(
            cluster="yz",
            namespace_name=first["namespace"],
            pod_name=first["pod"],
            container_name=first.get("container", "worker"),
            model_key="model-a",
            display_name="模型 A",
        ),
        ModelBinding(
            cluster="yz",
            namespace_name=second["namespace"],
            pod_name=second["pod"],
            container_name=second["container"],
            model_key="model-b",
            display_name="模型 B",
        ),
    ]
    await service.store.apply_bindings(mappings)
    await service.store.apply_bindings(mappings)
    result = await service.statistics(StatisticsRequest(cluster="yz"))
    assert result["summary"]["gpu_count"] == len(source.fixture["yz"])
    known = [m for m in result["business_models"] if m["model_key"]]
    assert len(known) == 2 and all(m["shared_gpu_count"] >= 1 for m in known)
    assert sum(m["allocated_raw"] for m in result["business_models"]) == result["summary"]["allocated_raw"]
    assert all("utilization_pct" not in m for m in result["business_models"])
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM resource_model_binding")).fetchone())[0] == 2
    await service.store.apply_bindings([])
    async with store.pool.connection() as c:
        assert (
            await (
                await c.execute("SELECT count(*) FROM resource_model_binding WHERE effective_until IS NULL")
            ).fetchone()
        )[0] == 0
        assert (await (await c.execute("SELECT count(*) FROM resource_model_binding")).fetchone())[0] == 2


async def test_idempotent_rollups_keep_sum_extrema_and_counts_after_pruning(domain):
    store, _source, _history, _metrics, service = domain
    ts = int(time.time()) // 3600 * 3600 - 3600
    inventory = {("fixture-node", "GPU-ROLLUP")}
    async with store.pool.connection() as conn:
        for at, value in ((ts, 10), (ts + 60, 20), (ts, 15)):
            records = {
                m: {"values": [{"node": "fixture-node", "device_uuid": "GPU-ROLLUP", "value": value}]}
                for m in ("gpu_util", "gpu_memory", "gpu_memory_free")
            }
            await service.store.capture(conn, "yz", at, inventory, records, {})
        rows = await (
            await conn.execute(
                "SELECT grain,sum_value,min_value,max_value,valid_points FROM resource_metric_bucket WHERE device_uuid='GPU-ROLLUP' AND metric='gpu_util' ORDER BY grain,ts"
            )
        ).fetchall()
    hourly = next(r for r in rows if r[0] == 3600)
    assert hourly[1:] == (35, 15, 20, 2)
    await service.store.prune(now=ts + 3 * 86400)
    async with store.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT grain,sum_value,min_value,max_value,valid_points FROM resource_metric_bucket WHERE device_uuid='GPU-ROLLUP' AND metric='gpu_util' ORDER BY grain"
            )
        ).fetchall()
    assert all(r[0] != 60 for r in rows)
    assert next(r for r in rows if r[0] == 3600)[1:] == hourly[1:]


async def test_bounded_backfill_and_history_keep_holes_without_source_reads(domain):
    _store, source, history, _metrics, service = domain
    for _ in range(3):
        await fill_recent(history, "yz")
    await fill_hours(history, "yz")

    async def forbidden(*args, **kwargs):
        raise AssertionError("history read contacted source")

    source.range_query = forbidden
    d = (await service.statistics(StatisticsRequest(cluster="yz")))["devices"][0]
    result = await service.history_for_device(
        DeviceHistoryRequest(cluster="yz", node=d["node"], device_uuid=d["device_uuid"])
    )
    util = next(s for s in result["series"] if s["metric"] == "gpu_util")["points"]
    assert len(util) == 60 and all(p["valid_points"] == 1 and p["mean"] == 2 for p in util)
    weekly = await service.history_for_device(
        DeviceHistoryRequest(cluster="yz", node=d["node"], device_uuid=d["device_uuid"], window="1w")
    )
    points = weekly["series"][0]["points"]
    assert weekly["grain"] == 3600 and len(points) == 168
    assert any(p["mean"] is None for p in points) and any(p["valid_points"] == 60 for p in points)
    with pytest.raises(SourceError):
        await service.history_for_device(
            DeviceHistoryRequest(cluster="yz", node="no-node", device_uuid="no-gpu")
        )


async def test_resource_api_rejects_scope_sql_and_cross_origin(domain):
    _store, source, _history, _metrics, service = domain
    config = source.settings.model_copy(
        update={"history_database_url": SecretStr(""), "operations_metrics_enabled": False}
    )
    app = create_app(config)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        app.state.resource_statistics = service
        assert (await client.post("/api/v1/resource-statistics", json={"cluster": "yz"})).status_code == 200
        assert (await client.post("/api/v1/resource-statistics", json={"sql": "select 1"})).status_code == 422
        assert (
            await client.post("/api/v1/resource-statistics", json={}, headers={"Origin": "https://bad"})
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/resource-statistics",
                content="x" * 5000,
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 413
        service.settings.insight_allowed_clusters = ["yz"]
        assert (await client.post("/api/v1/resource-statistics", json={"cluster": "zw"})).status_code == 403


@pytest.mark.parametrize("failure", ["eff_capacity", "ops_gpu_util"])
async def test_failed_current_collection_never_reuses_good_history_as_current(domain, failure):
    store, source, _history, collector, service = domain
    original = source.range_query

    async def failed(cluster, template, *args, **kwargs):
        if template == failure:
            raise SourceError("fixture source failure")
        return await original(cluster, template, *args, **kwargs)

    source.range_query = failed
    await collector.collect("yz")
    current = await service.statistics(StatisticsRequest(cluster="yz"))
    assert current["summary"]["allocated_raw"] is not None
    assert current["summary"]["utilization_pct"] is None
    assert current["summary"]["usage_observed_gpus"] == 0
    assert current["complete"] is False
    async with store.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM resource_metric_bucket WHERE cluster='yz' AND metric='gpu_util' AND grain=60 AND valid_points=1"
            )
        ).fetchone()
    assert row[0] > 0  # Previously observed history is retained; it is not a fresh current result.


async def test_asset_temperature_and_server_ready_keep_zero_and_missing_distinct(domain):
    from psycopg.types.json import Jsonb

    store, source, _history, _metrics, service = domain
    gpu = source.fixture["yz"][0]
    at = int(time.time()) // 60 * 60
    async with store.pool.connection() as c:
        for metric, values in (
            ("gpu_temp", [{"node": gpu["node"], "device_uuid": gpu["uuid"], "value": 0}]),
            ("node_ready", [{"node": gpu["node"], "device_uuid": None, "value": 0}]),
        ):
            await c.execute(
                "UPDATE operations_metric_snapshot SET evaluated_at=%s,attempted_at=now(),payload=%s WHERE cluster='yz' AND metric=%s",
                (
                    at,
                    Jsonb(
                        {
                            "status": "partial",
                            "values": values,
                            "expected_entities": len(source.fixture["yz"]),
                            "rejected_series": 0,
                            "observed_series": len(values),
                        }
                    ),
                    metric,
                ),
            )
    result = await service.statistics(StatisticsRequest(cluster="yz"))
    device = next(d for d in result["devices"] if d["device_uuid"] == gpu["uuid"])
    assert device["temperature_c"] == 0
    assert result["temperature_threshold_c"] == source.settings.operations_gpu_temperature_c
    assert next(s for s in result["servers"] if s["node"] == gpu["node"])["node_ready"] is False
    assert all(s["node_ready"] is None for s in result["servers"] if s["node"] != gpu["node"])
    assert result["complete"]  # Optional temperature/Ready coverage cannot erase the core GPU ledger.
    async with store.pool.connection() as c:
        await c.execute(
            "UPDATE operations_metric_snapshot SET evaluated_at=%s WHERE cluster='yz' AND metric='gpu_temp'",
            (at - 600,),
        )
    result = await service.statistics(StatisticsRequest(cluster="yz"))
    assert all(d["temperature_c"] is None for d in result["devices"])


async def test_catalog_maps_current_database_allocations_without_splitting_or_source_queries(domain):
    from app.resources.catalog import BusinessCatalog

    _store, source, _history, _metrics, service = domain
    before = await service.statistics(StatisticsRequest())
    pod = before["unmapped_workloads"][0]
    source.settings.resource_model_catalog = BusinessCatalog.model_validate(
        {
            "models": [
                {
                    "model_key": "demo-group",
                    "name": "演示双模型",
                    "category": "演示类别",
                    "model_names": ["v1", "v2"],
                    "selectors": [{"cluster": pod["cluster"], "field": "pod_name", "value": pod["pod"]}],
                }
            ]
        }
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("catalog read must not query Prometheus")

    source.instant_query = forbidden
    result = await service.statistics(StatisticsRequest())
    group = next(m for m in result["business_models"] if m["model_key"] == "demo-group")
    assert group["category"] == "演示类别" and group["model_names"] == ["v1", "v2"]
    assert group["allocated_raw"] == pod["allocated_raw"]
    assert group["gpu_count"] == pod["gpu_count"]
    assert len(group["workloads"]) == 1
    assert result["unmapped_workload_count"] == before["unmapped_workload_count"] - 1
    assert sum(m["allocated_raw"] for m in result["business_models"]) == sum(
        m["allocated_raw"] for m in before["business_models"]
    )
