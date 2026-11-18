"""Bounded host observations, joined only to verified GPU nodes; DMI serials never persist."""

import asyncio
import json
import math
import re
import time
from collections import defaultdict
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from app.core.config import CLUSTERS
from app.operations.hosts_extra import SOURCES as EXTRA_SOURCES

TEMPLATES: tuple[str, ...] = (
    "host_cpu",
    "host_cpu_count",
    "host_cores",
    "host_memory_total",
    "host_memory_available",
    "host_dmi",
)
BASE_TEMPLATES = TEMPLATES
TEMPLATES = BASE_TEMPLATES + tuple(EXTRA_SOURCES)


def host_query(cluster, template, device=None):
    if template in EXTRA_SOURCES:
        from app.operations.hosts_extra import query

        return query(cluster, template, device)
    scope = "cluster=~" + json.dumps("^$|" + CLUSTERS[cluster][1])
    name = {
        "host_cpu": "node_cpu_seconds_total",
        "host_cpu_count": "node_cpu_seconds_total",
        "host_cores": "machine_cpu_cores",
        "host_memory_total": "node_memory_MemTotal_bytes",
        "host_memory_available": "node_memory_MemAvailable_bytes",
        "host_dmi": "node_dmi_info",
    }[template]
    metric = name + "{" + scope + (',mode="idle"' if template in ("host_cpu", "host_cpu_count") else "") + "}"
    if template != "host_cores":
        pattern = device[0] if device else "__no_verified_gpu_host__"
        metric = metric[:-1] + ",instance=~" + json.dumps(pattern) + "}"
    fresh = f"({metric} and (time()-timestamp({metric})<=120) and (time()-timestamp({metric})>=0))"
    if template == "host_cpu":
        return f"100*(1-avg without(cpu,mode) (rate({metric}[5m]) and {fresh}))"
    if template == "host_cpu_count":
        return f"count without(cpu,mode) {fresh}"
    return fresh


def host_aliases(inventory, node_info):
    nodes = {n for n, _ in inventory}
    aliases = defaultdict(set)
    for node in nodes:
        aliases[node].add(node)
    for labels, value in node_info:
        if value != 1:
            continue
        candidates = {labels.get("node"), labels.get("internal_ip")}
        matches = candidates & nodes
        for alias in candidates - {None, ""}:
            aliases[alias].update(matches)
    return nodes, aliases


def normalize_hosts(cluster, at, inventory, node_info, payloads):
    nodes, aliases = host_aliases(inventory, node_info)
    values: dict[str, defaultdict[str, set]] = {n: defaultdict(set) for n in nodes}
    quality = {}
    for template, payload in payloads.items():
        if template in EXTRA_SOURCES:
            continue
        try:
            if (
                isinstance(payload, BaseException)
                or payload.get("warnings")
                or payload.get("infos")
                or payload.get("status") != "success"
            ):
                raise ValueError()
            series = payload["data"]["result"]
            if not isinstance(series, list) or len(series) > 1000:
                raise ValueError()
            quality[template] = "missing" if not series else "observed"
            for row in series:
                labels = row.get("metric", {})
                if labels.get("cluster") not in (None, "", CLUSTERS[cluster][1]):
                    continue
                if len(row.get("values", [])) != 1 or row["values"][0][0] != at:
                    raise ValueError()
                value = float(row["values"][0][1])
                if not math.isfinite(value):
                    continue
                candidates = set()
                for key in ("node", "nodename", "instance", "internal_ip"):
                    label = labels.get(key)
                    if label:
                        host = urlsplit("//" + label).hostname if ":" in label else label
                        candidates.update(aliases.get(host, set()))
                if len(candidates) != 1:
                    continue
                node = next(iter(candidates))
                if template == "host_dmi":
                    model = labels.get("product_name")
                    vendor = labels.get("system_vendor")
                    if value == 1 and model and len(model) <= 200 and (not vendor or len(vendor) <= 200):
                        values[node][template].add((model, vendor))
                elif (
                    value >= 0
                    and (template != "host_cpu" or value <= 100)
                    and (
                        template not in ("host_cpu_count", "host_cores") or value == int(value) and value > 0
                    )
                ):
                    values[node][template].add(value)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            quality[template] = "error"
    result = []
    for node in sorted(nodes):

        def one(key, node=node):
            samples = values[node][key]
            return next(iter(samples)) if len(samples) == 1 and quality.get(key) != "error" else None

        cores, count = one("host_cores"), one("host_cpu_count")
        total, available = one("host_memory_total"), one("host_memory_available")
        if total is not None and (total <= 0 or total > 2**60):
            total = None
        if available is not None and (total is None or available > total):
            available = None
        dmi = one("host_dmi")
        cpu = one("host_cpu") if cores and count == cores else None
        result.append(
            {
                "node": node,
                "cpu_cores": cores,
                "cpu_utilization_pct": cpu,
                "memory_total_bytes": total,
                "memory_available_bytes": available,
                "memory_used_bytes": total - available
                if total is not None and available is not None
                else None,
                "server_model": dmi[0] if dmi else None,
                "server_vendor": dmi[1] if dmi else None,
                "metric_statuses": {
                    key: (
                        "ready"
                        if one(key) is not None
                        else quality.get(key, "missing")
                        if quality.get(key) != "observed"
                        else "unmapped"
                    )
                    for key in BASE_TEMPLATES
                },
                "evaluated_at": at,
            }
        )
    from app.operations.hosts_extra import normalize

    extra = normalize(cluster, at, nodes, aliases, payloads)
    for row in result:
        row.update(extra[row["node"]])
        effective = {
            "host_cpu": row["cpu_utilization_pct"],
            "host_memory_total": row["memory_total_bytes"],
            "host_memory_available": row["memory_available_bytes"],
        }
        for key, value in effective.items():
            if value is None and row["metric_statuses"][key] == "ready":
                row["metric_statuses"][key] = "unverified"
    return result


async def collect_hosts(history, cluster, at, inventory, node_info):
    client = history.resources.client
    _, aliases = host_aliases(inventory, node_info)
    pattern = (
        "(" + "|".join(re.escape(a) for a in sorted(aliases)) + ")(:[0-9]+)?"
        if aliases
        else "__no_verified_gpu_host__"
    )
    if len(pattern) > 8000:
        raise ValueError("host scope exceeds budget")
    payloads = await asyncio.gather(
        *(client.range_query(cluster, key, at, at, 60, (pattern, "")) for key in TEMPLATES),
        return_exceptions=True,
    )
    rows = normalize_hosts(cluster, at, inventory, node_info, dict(zip(TEMPLATES, payloads)))
    async with history.store.pool.connection() as conn:
        # Replace the current cluster inventory atomically. Failure yields unknown, never stale success.
        await conn.execute("DELETE FROM node_metric_snapshot WHERE cluster=%s", (cluster,))
        for row in rows:
            await conn.execute(
                "INSERT INTO node_metric_snapshot(cluster,node,evaluated_at,payload) VALUES(%s,%s,%s,%s)",
                (cluster, row["node"], at, Jsonb(row)),
            )
        from app.operations.host_history import HostHistoryService

        service = HostHistoryService(history, client.settings)
        await service.capture(conn, cluster, at, rows)
        await service.rollup(conn, cluster, at, at)


async def read_hosts(history, clusters):
    if not history:
        return []
    async with history.store.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT cluster,node,evaluated_at,extract(epoch from attempted_at),payload FROM node_metric_snapshot WHERE cluster=ANY(%s) ORDER BY cluster,node LIMIT 1000",
                (clusters,),
            )
        ).fetchall()
    result = []
    for cluster, node, at, attempt, payload in rows:
        stale = time.time() - at > 180 or time.time() - float(attempt) > 180
        data = dict(payload)
        if stale:
            for field in ("metric_statuses", "extended_metric_statuses"):
                data[field] = {key: "stale" for key in data.get(field, {})}
            data.update(load1=None, filesystems=[], networks=[], kubernetes=None)
            for key in (
                "cpu_cores",
                "cpu_utilization_pct",
                "memory_total_bytes",
                "memory_available_bytes",
                "memory_used_bytes",
                "server_model",
                "server_vendor",
            ):
                data[key] = None
        result.append({"cluster": cluster, **data, "status": "stale" if stale else "observed"})
    return result


def mock_hosts(fixture, cluster, template, start, end, step):
    rows = []
    for i, node in enumerate(sorted({g["node"] for g in fixture[cluster]})):
        labels = {"instance": node + ":9100"}
        if template in EXTRA_SOURCES:
            from app.operations.hosts_extra import mock

            for extra_labels, value in mock(template):
                rows.append(
                    {
                        "metric": {
                            **labels,
                            "node": node,
                            **(
                                {"cluster": CLUSTERS[cluster][1]} if template.startswith("host_kube_") else {}
                            ),
                            **extra_labels,
                        },
                        "values": [[at, str(value)] for at in range(start, end + 1, step)],
                    }
                )
            continue
        if template == "host_dmi":
            labels.update(product_name="MOCK-SERVER", system_vendor="MOCK-VENDOR")
        value = {
            "host_cpu": 25 + i,
            "host_cpu_count": 64,
            "host_cores": 64,
            "host_memory_total": 256 * 2**30,
            "host_memory_available": 128 * 2**30,
            "host_dmi": 1,
        }[template]
        rows.append({"metric": labels, "values": [[at, str(value)] for at in range(start, end + 1, step)]})
    return {"status": "success", "data": {"resultType": "matrix", "result": rows}}
