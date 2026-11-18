"""Shared, database-only statistics for memory, devices, machines and explicit model ownership."""

import time
from collections import defaultdict

from app.clients.prometheus import SourceError
from app.core.time_ranges import range_seconds
from app.history.snapshots import SnapshotService
from app.resources.store import METRICS, ResourceStore

GRAINS = {"1h": 60, "1d": 300, "1w": 3600, "1mo": 3600, "3mo": 3600}


def total(values):
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def aggregate(devices):
    used = total(d["physical_used_mib"] for d in devices)
    free = total(d["physical_free_mib"] for d in devices)
    physical = used + free if used is not None and free is not None else None
    util = total(d["utilization_pct"] for d in devices)
    return {
        "gpu_count": len(devices),
        "machine_count": len({(d["cluster"], d["node"]) for d in devices}),
        "hami_capacity_raw": total(d["hami_capacity_raw"] for d in devices),
        "allocated_raw": total(d["allocated_raw"] for d in devices),
        "unallocated_raw": total(d["unallocated_raw"] for d in devices),
        "physical_used_mib": used,
        "physical_free_mib": free,
        "physical_observed_total_mib": physical,
        "physical_usage_ratio": used / physical if physical and used is not None else None,
        "utilization_pct": util / len(devices) if util is not None else None,
        "usage_observed_gpus": sum(d["utilization_pct"] is not None for d in devices),
    }


def unmapped_workloads(allocations, mapping, limit=500):
    """Expose real workload identities without inventing business-model ownership."""
    groups = defaultdict(list)
    for row in allocations:
        if (row.cluster, row.namespace, row.pod, row.container) not in mapping:
            groups[(row.cluster, row.namespace, row.pod)].append(row)
    items = []
    for (cluster, namespace, pod), rows in sorted(groups.items()):
        ids = sorted({(a.cluster, a.node, a.device_uuid) for a in rows})
        items.append(
            {
                "cluster": cluster,
                "namespace": namespace,
                "pod": pod,
                "model_key": None,
                "attribution": "unknown",
                "allocated_raw": total(a.allocated_raw for a in rows),
                "gpu_count": len(ids),
                "machine_count": len({a.node for a in rows}),
                "container_count": len({a.container for a in rows}),
                "associated_devices": [{"cluster": c, "node": n, "device_uuid": u} for c, n, u in ids[:100]],
                "devices_truncated": len(ids) > 100,
            }
        )
    return items[:limit], len(items)


class ResourceStatisticsService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings
        self.store = ResourceStore(history.store) if history else None

    def clusters(self, cluster):
        selected = ["yz", "zw"] if cluster == "all" else [cluster]
        if not set(selected).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准查询")
        if not self.history:
            raise SourceError("资源统计需要 PostgreSQL 数据库")
        return selected

    async def statistics(self, query):
        clusters = self.clusters(query.cluster)
        now = time.time()
        snapshots = SnapshotService(self.history.resources, self.history.store)
        assert self.store is not None
        async with self.store.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            identities = await (
                await conn.execute(
                    "SELECT cluster,node,device_uuid,gpu_model,driver_version,last_seen FROM resource_device WHERE cluster=ANY(%s)",
                    (clusters,),
                )
            ).fetchall()
            bindings = await (
                await conn.execute(
                    """SELECT b.cluster,b.namespace_name,b.pod_name,b.container_name,b.model_key,m.display_name
             FROM resource_model_binding b JOIN resource_business_model m USING(model_key)
             WHERE b.cluster=ANY(%s) AND b.effective_from<=%s AND (b.effective_until IS NULL OR b.effective_until>%s)""",
                    (clusters, int(now), int(now)),
                )
            ).fetchall()
        meta = {r[:3]: r[3:] for r in identities}
        from app.operations.metrics import MetricsService

        metric_snapshots = [
            m
            for m in await MetricsService(self.history, self.settings).read(clusters)
            if m["key"] in (*METRICS, "gpu_temp", "node_ready")
        ]
        metric_times = {(m["cluster"], m["key"]): m["evaluated_at"] for m in metric_snapshots}
        facts = {
            (m["cluster"], v["node"], v["device_uuid"], m["key"]): (m["evaluated_at"], v["value"])
            for m in metric_snapshots
            if m["status"] in ("ready", "partial")
            for v in m["values"]
        }
        mapping = {r[:4]: r[4:] for r in bindings}
        devices = []
        sources = []
        allocations = []
        for cid in clusters:
            c = snapshots.present(cid, now, await self.history.store.snapshot(cid), True)
            sources.append({"cluster": cid, "status": c.status, "quota_sampled_at": c.sampled_at})
            if c.status not in ("healthy", "degraded"):
                continue
            allocations.extend(c.workloads)
            for g in c.gpus:
                key = (g.cluster, g.node, g.device_uuid)
                metadata = meta.get(key, (None, None, None))
                data = {}
                for metric in METRICS:
                    row = facts.get((*key, metric))
                    data[metric] = row[1] if row else None
                used, free = data["gpu_memory"], data["gpu_memory_free"]
                devices.append(
                    {
                        "cluster": g.cluster,
                        "node": g.node,
                        "device_uuid": g.device_uuid,
                        "gpu_model": metadata[0],
                        "driver_version": metadata[1],
                        "usage_sampled_at": metric_times.get((g.cluster, "gpu_util")),
                        "quota_sampled_at": g.sampled_at,
                        "hami_capacity_raw": g.capacity_raw,
                        "allocated_raw": g.allocated_raw,
                        "unallocated_raw": g.unallocated_raw,
                        "utilization_pct": data["gpu_util"],
                        "temperature_c": facts.get((*key, "gpu_temp"), (None, None))[1],
                        "temperature_sampled_at": metric_times.get((g.cluster, "gpu_temp")),
                        "physical_used_mib": used,
                        "physical_free_mib": free,
                        "physical_observed_total_mib": used + free
                        if used is not None and free is not None
                        else None,
                    }
                )
        catalog = self.settings.resource_model_catalog
        metadata = {m.model_key: m for m in catalog.models}
        for a in allocations:
            identity = (a.cluster, a.namespace, a.pod, a.container)
            if identity not in mapping:
                model = catalog.resolve(*identity)
                if model:
                    mapping[identity] = (model.model_key, model.name)
        machines = defaultdict(list)
        gpu_models = defaultdict(list)
        for d in devices:
            machines[(d["cluster"], d["node"])].append(d)
            gpu_models[d["gpu_model"]].append(d)
        model_workloads = defaultdict(list)
        gpu_owners = defaultdict(set)
        for a in allocations:
            assignment = mapping.get((a.cluster, a.namespace, a.pod, a.container))
            model_key = assignment[0] if assignment else None
            model_workloads[model_key].append(a)
            gpu_owners[(a.cluster, a.node, a.device_uuid)].add(model_key)
        business = []
        for owner_key, rows in model_workloads.items():
            ids = {(a.cluster, a.node, a.device_uuid) for a in rows}
            name = (
                next((v[1] for v in mapping.values() if v[0] == owner_key), "归属未知")
                if owner_key
                else "归属未知"
            )
            business.append(
                {
                    "model_key": owner_key,
                    "name": name,
                    "category": metadata[owner_key].category if owner_key in metadata else "未分类",
                    "description": metadata[owner_key].description if owner_key in metadata else "",
                    "attributes": metadata[owner_key].attributes if owner_key in metadata else {},
                    "model_names": metadata[owner_key].model_names if owner_key in metadata else [],
                    "workloads": [
                        {"cluster": c, "namespace": n, "pod": p}
                        for c, n, p in sorted({(a.cluster, a.namespace, a.pod) for a in rows})[:100]
                    ],
                    "workloads_truncated": len({(a.cluster, a.namespace, a.pod) for a in rows}) > 100,
                    "allocated_raw": total(a.allocated_raw for a in rows),
                    "gpu_count": len(ids),
                    "machine_count": len({(a.cluster, a.node) for a in rows}),
                    "shared_gpu_count": sum(len(gpu_owners[i]) > 1 for i in ids),
                    "container_count": len({(a.cluster, a.namespace, a.pod, a.container) for a in rows}),
                    "associated_devices": [
                        {"cluster": i[0], "node": i[1], "device_uuid": i[2]} for i in sorted(ids)[:100]
                    ],
                    "devices_truncated": len(ids) > 100,
                    "attribution": "explicit_name_mapping" if owner_key else "unknown",
                }
            )
        unknown_workloads, unknown_count = unmapped_workloads(allocations, mapping)
        from app.operations.hosts import read_hosts

        hosts = {(h["cluster"], h["node"]): h for h in await read_hosts(self.history, clusters)}
        return {
            "mode": self.settings.data_mode,
            "database_read_at": now,
            "units": self.history.resources.units().model_dump(),
            "sources": sources,
            "quota_complete": all(s["status"] == "healthy" for s in sources),
            "metric_statuses": [
                {
                    "cluster": m["cluster"],
                    "metric": m["key"],
                    "status": m["status"],
                    "evaluated_at": m["evaluated_at"],
                }
                for m in metric_snapshots
            ],
            "complete": bool(devices)
            and all(m["status"] == "ready" for m in metric_snapshots if m["key"] in METRICS)
            and all(s["status"] == "healthy" for s in sources)
            and all(
                d["utilization_pct"] is not None
                and d["physical_used_mib"] is not None
                and d["physical_free_mib"] is not None
                for d in devices
            ),
            "summary": aggregate(devices),
            "devices": devices,
            "temperature_threshold_c": self.settings.operations_gpu_temperature_c,
            "host_collection_enabled": self.settings.host_metrics_enabled,
            "servers": [
                {
                    "cluster": cid,
                    "node": node,
                    "host": hosts.get((cid, node)),
                    "node_ready": bool(facts[(cid, node, None, "node_ready")][1])
                    if (cid, node, None, "node_ready") in facts
                    else None,
                }
                for cid, node in sorted(set(hosts) | set(machines))
            ],
            "machines": [
                {"cluster": key[0], "name": key[1], "host": hosts.get(key), **aggregate(rows)}
                for key, rows in machines.items()
            ],
            "gpu_models": [
                {"name": key or "型号未知", **aggregate(rows)} for key, rows in gpu_models.items()
            ],
            "business_models": business,
            "unmapped_workloads": unknown_workloads,
            "unmapped_workload_count": unknown_count,
            "unmapped_workloads_truncated": unknown_count > len(unknown_workloads),
            "limitations": [
                "物理显存统计使用 DCGM 已用+剩余，不与 HAMi 虚拟额度混算。",
                "模型关联卡数可交叉，不能相加作为物理卡总数；整卡利用率不归因到单个模型。",
            ],
        }

    async def history_for_device(self, query):
        clusters = self.clusters(query.cluster)
        if len(clusters) != 1:
            raise SourceError("单卡历史须指定一个集群")
        grain = GRAINS[query.window]
        now = int(time.time())
        end = (now - 120) // grain * grain - grain
        seconds = range_seconds(query.window, end)
        start = end - seconds + grain
        assert self.store is not None
        async with self.store.pool.connection() as conn:
            known = await (
                await conn.execute(
                    "SELECT 1 FROM resource_device WHERE cluster=%s AND node=%s AND device_uuid=%s",
                    (clusters[0], query.node, query.device_uuid),
                )
            ).fetchone()
            if not known:
                raise SourceError("未找到对应设备历史")
            rows = await (
                await conn.execute(
                    "SELECT metric,ts,sum_value,min_value,max_value,valid_points FROM resource_metric_bucket WHERE cluster=%s AND node=%s AND device_uuid=%s AND grain=%s AND ts BETWEEN %s AND %s ORDER BY ts",
                    (clusters[0], query.node, query.device_uuid, grain, start, end),
                )
            ).fetchall()
        by_time = {(r[0], r[1]): r for r in rows}
        series = []
        for metric in METRICS:
            points = []
            for ts in range(start, end + 1, grain):
                row = by_time.get((metric, ts))
                valid = row[5] if row else 0
                expected = grain // 60
                points.append(
                    {
                        "timestamp": ts,
                        "mean": row[2] / valid if row is not None and valid == expected else None,
                        "min": row[3] if row else None,
                        "max": row[4] if row else None,
                        "valid_points": valid,
                        "expected_points": expected,
                        "coverage": valid / expected,
                    }
                )
            series.append(
                {"metric": metric, "unit": "%" if metric == "gpu_util" else "MiB", "points": points}
            )
        return {
            "cluster": clusters[0],
            "node": query.node,
            "device_uuid": query.device_uuid,
            "start": start,
            "end": end,
            "grain": grain,
            "series": series,
            "database_read_at": time.time(),
        }
