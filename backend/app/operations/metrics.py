"""Fixed source collector; page reads never trigger Prometheus requests."""

import asyncio
import math
import time
from collections import defaultdict

from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

from app.core.config import CLUSTERS
from app.operations.catalog import CATALOG
from app.resources.store import ResourceStore


def samples(payload, cluster, at):
    if isinstance(payload, BaseException) or payload.get("warnings") or payload.get("infos"):
        raise ValueError("source unavailable or partial")
    result = payload.get("data", {}).get("result")
    if not isinstance(result, list) or len(result) > 1000:
        raise ValueError("source response budget")
    for series in result:
        labels = series.get("metric", {})
        if labels.get("cluster") != CLUSTERS[cluster][1]:
            raise ValueError("source scope")
        points = series.get("values", [])
        if len(points) != 1 or points[0][0] != at:
            raise ValueError("source evaluation grid")
        value = float(points[0][1])
        if not math.isfinite(value):
            raise ValueError("source nonfinite")
        yield labels, value


def normalize(key, payload, cluster, at, inventory, node_info, limits=None):
    definition = limits or CATALOG[key]
    values = defaultdict(set)
    rejected = 0
    uuids = defaultdict(set)
    nodes = set()
    for node, uuid in inventory:
        uuids[uuid].add(node)
        nodes.add(node)
    aliases = defaultdict(set)
    for labels, value in node_info:
        if value == 1 and labels.get("node"):
            matches = nodes.intersection({labels["node"], labels.get("internal_ip")})
            aliases[labels["node"]].update(matches)
    raw = list(samples(payload, cluster, at))
    for labels, value in raw:
        if (
            not definition[3] <= value <= definition[4]
            or key in ("node_ready", "gpu_ecc", "gpu_xid")
            and value != int(value)
        ):
            rejected += 1
            continue
        if key == "node_ready":
            source = labels.get("node")
            matches = ({source} if source in nodes else set()) | aliases[source]
            if len(matches) != 1:
                rejected += 1
                continue
            entity = (next(iter(matches)), "")
        else:
            ids = {labels[n] for n in ("UUID", "device_uuid", "deviceuuid") if labels.get(n)}
            if len(ids) != 1 or len(uuids[next(iter(ids))]) != 1:
                rejected += 1
                continue
            uuid = next(iter(ids))
            # MIG partitions cannot be silently treated as whole GPUs.
            if labels.get("GPU_I_ID") or labels.get("GPU_I_PROFILE"):
                rejected += 1
                continue
            entity = (next(iter(uuids[uuid])), uuid)
        values[entity].add(value)
    valid = [
        {"node": node, "device_uuid": uuid or None, "value": next(iter(v))}
        for (node, uuid), v in values.items()
        if len(v) == 1
    ]
    rejected += sum(len(v) > 1 for v in values.values())
    expected = len(nodes) if key == "node_ready" else len(inventory)
    status = (
        "missing"
        if not raw
        else "unmapped"
        if not valid
        else "partial"
        if rejected or len(valid) != expected
        else "ready"
    )
    return {
        "status": status,
        "values": valid,
        "expected_entities": expected,
        "rejected_series": rejected,
        "observed_series": len(raw),
    }


class MetricsService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings
        self.pool = history.store.pool if history else None
        self.status = "disabled"
        self.running = False
        self.started_at = None
        self.last_tick_at = None
        self.last_tick_finished_at = None
        self.active_cluster = None
        self.phase = "idle"
        self.last_error = None
        self.cluster_runs = {}

    def diagnostics(self):
        return {
            "enabled": bool(self.pool)
            and any(
                (
                    self.settings.operations_metrics_enabled,
                    self.settings.host_metrics_enabled,
                    self.settings.workload_metrics_enabled,
                )
            ),
            "database_configured": bool(self.pool),
            "operations_metrics_enabled": self.settings.operations_metrics_enabled,
            "host_metrics_enabled": self.settings.host_metrics_enabled,
            "workload_metrics_enabled": self.settings.workload_metrics_enabled,
            "running": self.running,
            "status": self.status,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "last_tick_finished_at": self.last_tick_finished_at,
            "active_cluster": self.active_cluster,
            "phase": self.phase,
            "last_error": self.last_error,
            "clusters": {key: dict(value) for key, value in self.cluster_runs.items()},
        }

    async def collect(self, cluster, at=None):
        at = int(time.time()) // 60 * 60 if at is None else at
        self.active_cluster = cluster
        self.phase = "source_query"
        self.cluster_runs.setdefault(cluster, {})["last_collection_started_at"] = time.time()
        client = self.history.resources.client
        names = ["eff_capacity", "ops_node_info"] + ["ops_" + key for key in CATALOG]
        results = await asyncio.gather(
            *(client.range_query(cluster, name, at, at, 60) for name in names), return_exceptions=True
        )
        try:
            inventory = set()
            for labels, value in samples(results[0], cluster, at):
                ids = {labels[k] for k in ("device_uuid", "deviceuuid") if labels.get(k)}
                if value > 0 and labels.get("node") and len(ids) == 1:
                    inventory.add((labels["node"], next(iter(ids))))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            inventory = set()
        try:
            node_info = list(samples(results[1], cluster, at))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            node_info = []
        records = []
        for key, payload in zip(CATALOG, results[2:]):
            try:
                record = normalize(key, payload, cluster, at, inventory, node_info)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                record = {
                    "status": "error",
                    "values": [],
                    "expected_entities": len(inventory),
                    "rejected_series": 0,
                    "observed_series": 0,
                }
            records.append((cluster, key, at, Jsonb(record)))
        from app.resources.identity import device_metadata

        metadata = device_metadata(results[names.index("ops_gpu_memory")], cluster, at, inventory)
        assert self.pool is not None
        self.phase = "snapshot_write"
        async with self.pool.connection() as conn:
            await ResourceStore(self.history.store).capture(
                conn,
                cluster,
                at,
                inventory,
                {key: row[3].obj for key, row in zip(CATALOG, records)},
                metadata,
            )
            for row in records:
                await conn.execute(
                    "INSERT INTO operations_metric_snapshot(cluster,metric,evaluated_at,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(cluster,metric) DO UPDATE SET attempted_at=now(),evaluated_at=excluded.evaluated_at,payload=excluded.payload",
                    row,
                )
        self.cluster_runs[cluster]["last_snapshot_write_at"] = time.time()
        self.cluster_runs[cluster]["result_counts"] = {
            state: sum(row[3].obj["status"] == state for row in records)
            for state in ("ready", "partial", "missing", "unmapped", "error")
        }
        self.phase = "retention"
        await ResourceStore(self.history.store).prune()
        if self.settings.host_metrics_enabled:
            from app.operations.hosts import collect_hosts

            self.phase = "host_collection"
            await collect_hosts(self.history, cluster, at, inventory, node_info)
        if self.settings.workload_metrics_enabled:
            from app.workloads.service import collect

            self.phase = "workload_collection"
            await collect(self.history, cluster, at, inventory, node_info)
        self.status = "ok"

    async def tick(self):
        for cluster, lock in (("yz", 7490), ("zw", 7491)):
            if cluster not in self.settings.insight_allowed_clusters:
                continue
            self.active_cluster = cluster
            self.phase = "collection_lock"
            self.cluster_runs.setdefault(cluster, {})["last_tick_at"] = time.time()
            assert self.pool is not None
            async with self.pool.connection() as conn:
                locked = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock,))).fetchone()
                if not locked[0]:
                    self.cluster_runs[cluster]["last_outcome"] = "locked_by_other_collector"
                    continue
                try:
                    self.phase = "freshness_guard"
                    recent = await (
                        await conn.execute(
                            "SELECT max(attempted_at)>now()-interval '55 seconds' FROM operations_metric_snapshot WHERE cluster=%s",
                            (cluster,),
                        )
                    ).fetchone()
                    bootstrap_hosts = False
                    if self.settings.host_metrics_enabled:
                        host_row = await (
                            await conn.execute(
                                "SELECT EXISTS(SELECT 1 FROM node_metric_snapshot WHERE cluster=%s) AND EXISTS(SELECT 1 FROM host_history_coverage WHERE cluster=%s)",
                                (cluster, cluster),
                            )
                        ).fetchone()
                        bootstrap_hosts = not host_row[0]
                    bootstrap_workloads = False
                    if self.settings.workload_metrics_enabled:
                        exists = await (
                            await conn.execute(
                                "SELECT EXISTS(SELECT 1 FROM workload_resource_snapshot WHERE cluster=%s)",
                                (cluster,),
                            )
                        ).fetchone()
                        bootstrap_workloads = not exists[0]
                    if not recent[0] or bootstrap_hosts or bootstrap_workloads:
                        await self.collect(cluster)
                        from app.resources.backfill import fill_recent

                        self.phase = "gpu_recent_history"
                        await fill_recent(self.history, cluster)
                        from app.resources.coarse import fill_hours

                        self.phase = "gpu_coarse_history"
                        await fill_hours(self.history, cluster)
                        if self.settings.host_metrics_enabled:
                            from app.operations.host_history import fill_recent as fill_hosts

                            self.phase = "host_recent_history"
                            await fill_hosts(self.history, cluster)
                            from app.operations.host_samples import fill_points as fill_host_hours

                            coarse_lock = 7500 if cluster == "yz" else 7501
                            coarse_locked = await (
                                await conn.execute("SELECT pg_try_advisory_lock(%s)", (coarse_lock,))
                            ).fetchone()
                            if coarse_locked[0]:
                                try:
                                    self.phase = "host_hourly_history"
                                    await fill_host_hours(self.history, cluster, budget=24)
                                finally:
                                    await conn.execute("SELECT pg_advisory_unlock(%s)", (coarse_lock,))
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))

    async def run(self):
        self.running = True
        self.started_at = time.time()
        self.status = "running"
        try:
            while True:
                self.last_tick_at = time.time()
                self.active_cluster = None
                self.phase = "starting"
                try:
                    async with asyncio.timeout(60):
                        await self.tick()
                    self.status = "ok"
                    self.phase = "idle"
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - record categories, never error text or parameters
                    self.status = "error"
                    self.last_error = {
                        "at": time.time(),
                        "cluster": self.active_cluster,
                        "phase": self.phase,
                        "code": "timeout"
                        if isinstance(error, TimeoutError)
                        else "database"
                        if isinstance(error, (DatabaseError, PoolTimeout))
                        else "internal",
                        "sqlstate": error.sqlstate if isinstance(error, DatabaseError) else None,
                    }
                    if self.active_cluster:
                        self.cluster_runs.setdefault(self.active_cluster, {})["last_outcome"] = "error"
                finally:
                    self.last_tick_finished_at = time.time()
                await asyncio.sleep(60)
        finally:
            self.running = False
            self.status = "stopped"

    async def read(self, clusters):
        rows = []
        if self.pool:
            assert self.pool is not None
            async with self.pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT cluster,metric,evaluated_at,extract(epoch from attempted_at),payload FROM operations_metric_snapshot WHERE cluster=ANY(%s)",
                        (clusters,),
                    )
                ).fetchall()
        stored = {(r[0], r[1]): r for r in rows}
        result = []
        for cid in clusters:
            for key, (source, label, unit, *_) in CATALOG.items():
                row = stored.get((cid, key))
                payload = (
                    dict(row[4])
                    if row
                    else {"status": "not_collected", "values": [], "expected_entities": None}
                )
                if row and (time.time() - float(row[3]) > 180 or time.time() - row[2] > 180):
                    payload.update(status="stale", values=[])
                result.append(
                    {
                        "cluster": cid,
                        "key": key,
                        "source": source,
                        "label": label,
                        "unit": unit,
                        "evaluated_at": row[2] if row else None,
                        **payload,
                    }
                )
        return result


def alerts(metrics, temperature):
    result = []
    for metric in metrics:
        if metric["status"] not in ("ready", "partial"):
            continue
        for row in metric["values"]:
            key, value = metric["key"], row["value"]
            message = (
                f"GPU 温度达到 {value:g} °C（阈值 {temperature:g} °C）"
                if key == "gpu_temp" and value >= temperature
                else f"最近 XID 错误编号为 {value:g}，需核实发生时间"
                if key == "gpu_xid" and value > 0
                else f"累计双位 ECC 错误 {value:g} 次，不代表本次新增"
                if key == "gpu_ecc" and value > 0
                else "GPU 节点 Ready 条件为 false 或 unknown"
                if key == "node_ready" and value == 0
                else None
            )
            if message:
                result.append(
                    {
                        "cluster": metric["cluster"],
                        "metric": key,
                        "node": row["node"],
                        "device_uuid": row["device_uuid"],
                        "message": message,
                        "severity": "warning",
                        "evaluated_at": metric["evaluated_at"],
                    }
                )
    return result
