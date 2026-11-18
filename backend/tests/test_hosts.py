# ruff: noqa: F811
import time

from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.operations.hosts import TEMPLATES, host_query, mock_hosts, normalize_hosts, read_hosts


def fixture():
    inventory = {("gpu-node", "GPU-MOCK")}
    node_info = [({"node": "worker-one", "internal_ip": "gpu-node"}, 1)]
    data = {key: mock_hosts({"yz": [{"node": "gpu-node"}]}, "yz", key, 120, 120, 60) for key in TEMPLATES}
    return inventory, node_info, data


def test_host_units_identity_and_dmi_whitelist():
    inventory, info, data = fixture()
    data["host_dmi"]["data"]["result"][0]["metric"]["product_serial"] = "PRIVATE-SERIAL"
    row = normalize_hosts("yz", 120, inventory, info, data)[0]
    assert row["cpu_utilization_pct"] == 25 and row["cpu_cores"] == 64
    assert row["memory_used_bytes"] == 128 * 2**30 and row["server_model"] == "MOCK-SERVER"
    assert "PRIVATE-SERIAL" not in str(row)
    assert "instance=~" in host_query("yz", "host_cpu", ("gpu-node(:[0-9]+)?", ""))
    assert "[5m]" in host_query("yz", "host_cpu") and "__no_verified_gpu_host__" in host_query(
        "yz", "host_cpu"
    )


def test_conflicts_partial_cpu_and_invalid_memory_remain_unknown():
    inventory, info, data = fixture()
    data["host_cpu_count"]["data"]["result"][0]["values"][0][1] = "32"
    data["host_memory_available"]["data"]["result"][0]["values"][0][1] = str(512 * 2**30)
    row = normalize_hosts("yz", 120, inventory, info, data)[0]
    assert row["cpu_utilization_pct"] is None and row["memory_used_bytes"] is None
    data["host_dmi"]["data"]["result"][0]["metric"]["cluster"] = "foreign-cluster"
    assert normalize_hosts("yz", 120, inventory, info, data)[0]["server_model"] is None
    data["host_cpu"] = RuntimeError("private")
    assert normalize_hosts("yz", 120, inventory, info, data)[0]["cpu_utilization_pct"] is None


async def test_host_collection_is_idempotent_and_reads_do_not_query_sources(domain):
    store, source, history, metrics, _service = domain
    source.settings.host_metrics_enabled = True
    at = int(time.time()) // 60 * 60
    await metrics.collect("yz", at)
    await metrics.collect("yz", at)

    async def forbidden(*args, **kwargs):
        raise AssertionError("read queried upstream")

    source.range_query = forbidden
    hosts = await read_hosts(history, ["yz"])
    assert hosts and all(h["server_model"] == "MOCK-SERVER" and h["cpu_cores"] == 64 for h in hosts)
    async with store.pool.connection() as c:
        await c.execute("UPDATE node_metric_snapshot SET evaluated_at=%s", (at - 3600,))
    assert all(
        h["status"] == "stale" and h["memory_total_bytes"] is None for h in await read_hosts(history, ["yz"])
    )


async def test_new_host_collection_bootstraps_under_existing_gpu_cooldown(domain):
    store, source, history, metrics, _service = domain
    source.settings.host_metrics_enabled = True
    async with store.pool.connection() as c:
        await c.execute("TRUNCATE node_metric_snapshot")
    await metrics.tick()
    hosts = await read_hosts(history, ["yz", "zw"])
    assert len(hosts) == len(
        {(cluster, g["node"]) for cluster in ("yz", "zw") for g in source.fixture[cluster]}
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("cooldown failed")

    original = source.range_query
    source.range_query = forbidden
    await metrics.tick()
    source.range_query = original
    # Upgrading an existing snapshot without host history must bootstrap despite the GPU cooldown.
    async with store.pool.connection() as c:
        await c.execute("TRUNCATE host_history_coverage,host_metric_bucket")
    await metrics.tick()
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT count(*) FROM host_history_coverage")).fetchone())[0] > 0
