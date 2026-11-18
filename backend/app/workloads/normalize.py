"""No name-only attribution: KSM Pod UID + current container ID gate actual metrics."""

import math
import re
from collections import defaultdict
from typing import Any

from app.core.config import CLUSTERS
from app.operations.hosts import host_aliases
from app.workloads.queries import SOURCES

FIELDS = (
    "cpu_usage_cores",
    "memory_working_set_bytes",
    "cpu_request_cores",
    "cpu_limit_cores",
    "memory_request_bytes",
    "memory_limit_bytes",
)


def normalize(cluster, at, inventory, node_info, payloads):
    nodes, aliases = host_aliases(inventory, node_info)
    parts = {}
    source_statuses = {}
    for template in SOURCES:
        try:
            payload = payloads[template]
            if (
                not isinstance(payload, dict)
                or payload.get("status") != "success"
                or payload.get("warnings")
                or payload.get("infos")
            ):
                raise ValueError()
            rows = payload["data"]["result"]
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError()
            parsed = []
            for row in rows:
                labels = row["metric"]
                point = row["values"]
                if labels.get("cluster") != CLUSTERS[cluster][1]:
                    continue
                if len(point) != 1 or point[0][0] != at:
                    raise ValueError()
                value = float(point[0][1])
                if not math.isfinite(value) or value < 0:
                    continue
                if any(
                    not isinstance(labels.get(k), str) or not 0 < len(labels[k]) <= 256
                    for k in ("namespace", "pod")
                ):
                    continue
                parsed.append((labels, value))
            parts[template] = parsed
            source_statuses[template] = "ready" if parsed else "missing"
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            parts[template] = []
            source_statuses[template] = "error"
    pods = defaultdict(set)
    for m, v in parts["wl_pods"]:
        matched = aliases.get(m.get("node"), set())
        if v == 1 and len(matched) == 1 and isinstance(m.get("uid"), str) and 0 < len(m["uid"]) <= 128:
            pods[(m["namespace"], m["pod"])].add((m["uid"], next(iter(matched))))
    containers = defaultdict(set)
    for m, v in parts["wl_containers"]:
        c = m.get("container")
        uid = m.get("uid")
        if v == 1 and isinstance(c, str) and c not in ("", "POD") and len(c) <= 256:
            containers[(m["namespace"], m["pod"], c)].add((uid, m.get("container_id", "")))
    index: dict[str, Any] = {t: defaultdict(list) for t in SOURCES}
    for t in ("wl_cpu", "wl_memory", "wl_requests", "wl_limits", "wl_running"):
        for m, v in parts[t]:
            index[t][(m["namespace"], m["pod"], m.get("container", ""))].append((m, v))
    result = []
    rejected = 0
    for (namespace, pod, container), identities in sorted(containers.items()):
        parent = pods.get((namespace, pod), set())
        if len(parent) != 1 or len(identities) != 1:
            rejected += 1
            continue
        uid, node = next(iter(parent))
        container_uid, runtime = next(iter(identities))
        if uid != container_uid or not isinstance(runtime, str) or len(runtime) > 256:
            rejected += 1
            continue
        rid = re.fullmatch(r"(?:docker|containerd|cri-o)://([a-f0-9]{64})", runtime)
        states = {}
        values = {}

        def metric(
            template,
            resource=None,
            unit=None,
            namespace=namespace,
            pod=pod,
            container=container,
            uid=uid,
            rid=rid,
        ):
            found = set()
            mismatched = False
            for m, v in index[template][(namespace, pod, container)]:
                if resource is not None:
                    if m.get("resource") != resource:
                        continue
                    if m.get("uid") != uid:
                        continue
                    if m.get("unit") != unit:
                        mismatched = True
                        continue
                else:
                    raw = m.get("id") or m.get("container_id") or ""
                    if (
                        not rid
                        or not isinstance(raw, str)
                        or not re.search(r"(?<![a-f0-9])" + re.escape(rid[1]) + r"(?![a-f0-9])", raw)
                    ):
                        continue
                found.add(v)
            status = (
                "error"
                if source_statuses[template] == "error"
                else "unit_conflict"
                if mismatched
                else "conflict"
                if len(found) > 1
                else "ready"
                if len(found) == 1
                else "unverified_identity"
                if resource is None and index[template][(namespace, pod, container)]
                else "missing"
            )
            return (next(iter(found)) if status == "ready" else None, status)

        for field, t, resource, unit in (
            ("cpu_usage_cores", "wl_cpu", None, None),
            ("memory_working_set_bytes", "wl_memory", None, None),
            ("cpu_request_cores", "wl_requests", "cpu", "core"),
            ("cpu_limit_cores", "wl_limits", "cpu", "core"),
            ("memory_request_bytes", "wl_requests", "memory", "byte"),
            ("memory_limit_bytes", "wl_limits", "memory", "byte"),
        ):
            values[field], states[field] = metric(t, resource, unit)
        running = {
            v for m, v in index["wl_running"][(namespace, pod, "")] if m.get("uid") == uid and v in (0, 1)
        }
        result.append(
            {
                "node": node,
                "namespace_name": namespace,
                "pod_name": pod,
                "container_name": container,
                "pod_uid": uid,
                "container_id": runtime or None,
                "running": bool(next(iter(running))) if len(running) == 1 else None,
                **values,
                "metric_statuses": states,
            }
        )
    metadata_ok = all(source_statuses[t] != "error" for t in ("wl_pods", "wl_containers"))
    status = (
        "error"
        if not metadata_ok
        else "partial"
        if rejected
        or any(v is None for r in result for v in (r["cpu_usage_cores"], r["memory_working_set_bytes"]))
        else "ready"
        if result
        else "empty"
    )
    return {
        "status": status,
        "rows": result[:500],
        "row_count": len(result),
        "truncated": len(result) > 500,
        "identity_rejected": rejected,
        "source_statuses": source_statuses,
        "expected_gpu_nodes": len(nodes),
        "evaluated_at": at,
    }
