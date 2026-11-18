import asyncio
import hashlib
import json
import re
import time
from typing import Any

from psycopg.types.json import Jsonb

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS
from app.operations.hosts import host_aliases
from app.workloads.normalize import FIELDS, normalize
from app.workloads.queries import SOURCES


async def collect(history, cluster, at, inventory, node_info):
    client = history.resources.client
    _, aliases = host_aliases(inventory, node_info)
    pattern = (
        "(" + "|".join(re.escape(a) for a in sorted(aliases)) + ")" if aliases else "__no_verified_gpu_node__"
    )
    if len(pattern) > 8000:
        raise SourceError("业务负载范围超过预算")
    payloads = await asyncio.gather(
        *(client.range_query(cluster, t, at, at, 60, (pattern, "")) for t in SOURCES), return_exceptions=True
    )
    data = normalize(cluster, at, inventory, node_info, dict(zip(SOURCES, payloads)))
    if not inventory:
        data.update(status="error", rows=[])
    if len(json.dumps(data).encode()) > 2_000_000:
        data.update(status="error", rows=[], truncated=True)
    async with history.store.pool.connection() as c:
        await c.execute(
            """INSERT INTO workload_resource_snapshot(cluster,evaluated_at,payload) VALUES(%s,%s,%s)
            ON CONFLICT(cluster) DO UPDATE SET evaluated_at=excluded.evaluated_at,attempted_at=now(),payload=excluded.payload""",
            (cluster, at, Jsonb(data)),
        )


class WorkloadService:
    def __init__(self, history, settings):
        self.history, self.settings = history, settings

    async def read(self, cluster):
        selected = ["yz", "zw"] if cluster == "all" else [cluster]
        if not set(selected).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("所选集群未获准查询")
        if not self.history:
            raise SourceError("业务资源需要 PostgreSQL 数据库")
        async with self.history.store.pool.connection() as c:
            await c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshots = await (
                await c.execute(
                    "SELECT cluster,evaluated_at,extract(epoch from attempted_at),payload FROM workload_resource_snapshot WHERE cluster=ANY(%s)",
                    (selected,),
                )
            ).fetchall()
            mapping = await (
                await c.execute(
                    """SELECT b.cluster,b.namespace_name,b.pod_name,b.container_name,b.model_key,m.display_name,b.effective_from
                FROM resource_model_binding b JOIN resource_business_model m USING(model_key)
                WHERE b.cluster=ANY(%s) AND b.effective_until IS NULL""",
                    (selected,),
                )
            ).fetchall()
        bindings = {r[:4]: r[4:] for r in mapping}
        stored = {r[0]: r for r in snapshots}
        sources: list[dict[str, Any]] = []
        containers = []
        for cid in selected:
            row = stored.get(cid)
            if not row:
                sources.append(
                    {
                        "cluster": cid,
                        "status": "not_collected",
                        "evaluated_at": None,
                        "row_count": 0,
                        "truncated": False,
                        "identity_rejected": 0,
                        "source_statuses": {},
                    }
                )
                continue
            data = dict(row[3])
            stale = time.time() - row[1] > 180 or time.time() - float(row[2]) > 180
            sources.append(
                {
                    "cluster": cid,
                    "status": "stale" if stale else data["status"],
                    "evaluated_at": row[1],
                    **{
                        k: data[k] for k in ("row_count", "truncated", "identity_rejected", "source_statuses")
                    },
                }
            )
            if stale or data["status"] == "error":
                continue
            for entry in data["rows"]:
                item = {**entry, "cluster": cid}
                model = bindings.get((cid, item["namespace_name"], item["pod_name"], item["container_name"]))
                item.update(
                    model_key=model[0] if model and model[2] <= row[1] else None,
                    model_name=model[1] if model and model[2] <= row[1] else None,
                )
                if item["model_key"] is None:
                    owner = self.settings.resource_model_catalog.resolve(
                        cid, item["namespace_name"], item["pod_name"], item["container_name"]
                    )
                    if owner:
                        item.update(model_key=owner.model_key, model_name=owner.name)
                containers.append(item)
        complete = all(
            s["status"] in ("ready", "partial") and not s["truncated"] and not s["identity_rejected"]
            for s in sources
        )
        summary = {
            f: sum(r[f] for r in containers)
            if complete and containers and all(r[f] is not None for r in containers)
            else None
            for f in FIELDS
        }
        coverage = {f: sum(r[f] is not None for r in containers) for f in FIELDS}
        return {
            "database_read_at": time.time(),
            "collection_enabled": self.settings.workload_metrics_enabled,
            "sources": sources,
            "containers": containers,
            "summary": summary,
            "coverage": coverage,
            "units": {f: "cores" if f.startswith("cpu_") else "bytes" for f in FIELDS},
            "scope": "regular_containers_on_gpu_nodes",
            "mode": self.settings.data_mode,
        }


def mock(fixture, cluster, template, start, end, step):
    rows = []
    seen = set()
    for gpu in fixture[cluster]:
        for a in gpu["allocations"]:
            key = (a["namespace"], a["pod"])
            if key in seen:
                continue
            seen.add(key)
            uid = "UID-" + a["pod"]
            rid = hashlib.sha256((cluster + uid).encode()).hexdigest()
            labels = {
                "cluster": CLUSTERS[cluster][1],
                "node": gpu["node"],
                "namespace": a["namespace"],
                "pod": a["pod"],
                "uid": uid,
                "container": "worker",
            }
            if template == "wl_containers":
                labels["container_id"] = "containerd://" + rid
            if template in ("wl_cpu", "wl_memory"):
                labels["id"] = "/kubepods/" + uid + "/" + rid
            vals = [(labels, 0.25 if template == "wl_cpu" else 2e9 if template == "wl_memory" else 1)]
            if template in ("wl_requests", "wl_limits"):
                vals = [
                    ({**labels, "resource": r, "unit": u}, v)
                    for r, u, v in [
                        ("cpu", "core", 1 if template == "wl_requests" else 2),
                        ("memory", "byte", 4e9 if template == "wl_requests" else 8e9),
                    ]
                ]
            if template == "wl_running":
                vals = [({k: v for k, v in labels.items() if k != "container"}, 1)]
            for m, v in vals:
                rows.append({"metric": m, "values": [[ts, str(v)] for ts in range(start, end + 1, step)]})
    return {"status": "success", "data": {"resultType": "matrix", "result": rows}}
