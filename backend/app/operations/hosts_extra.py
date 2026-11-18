"""Fixed node-exporter/KSM observations. No disk/interface sums or scheduler claims."""

import json
import math
from collections import defaultdict
from urllib.parse import urlsplit

from app.core.config import CLUSTERS

SOURCES = {
    "host_load1": ("node_load1", "", "load"),
    "host_filesystem_size": (
        "node_filesystem_size_bytes",
        'fstype!~"tmpfs|devtmpfs|overlay|squashfs|proc|sysfs|nsfs",mountpoint!~"/(run|dev|proc|sys)(/.*)?|/var/lib/(docker|kubelet|containers)(/.*)?"',
        "bytes",
    ),
    "host_filesystem_available": (
        "node_filesystem_avail_bytes",
        'fstype!~"tmpfs|devtmpfs|overlay|squashfs|proc|sysfs|nsfs",mountpoint!~"/(run|dev|proc|sys)(/.*)?|/var/lib/(docker|kubelet|containers)(/.*)?"',
        "bytes",
    ),
    "host_network_receive": (
        "node_network_receive_bytes_total",
        'device!~"lo|veth.*|cali.*|cni.*|flannel.*|docker.*|br-.*|kube-ipvs.*"',
        "bytes/s",
    ),
    "host_network_transmit": (
        "node_network_transmit_bytes_total",
        'device!~"lo|veth.*|cali.*|cni.*|flannel.*|docker.*|br-.*|kube-ipvs.*"',
        "bytes/s",
    ),
    "host_kube_allocatable_cpu": ("kube_node_status_allocatable", 'resource="cpu",unit="core"', "cores"),
    "host_kube_allocatable_memory": (
        "kube_node_status_allocatable",
        'resource="memory",unit="byte"',
        "bytes",
    ),
    "host_kube_unschedulable": ("kube_node_spec_unschedulable", "", "boolean"),
    "host_kube_pressure": (
        "kube_node_status_condition",
        'condition=~"MemoryPressure|DiskPressure|PIDPressure"',
        "condition",
    ),
}


def query(cluster, template, device):
    name, filters, _ = SOURCES[template]
    scope = (
        'cluster="' + CLUSTERS[cluster][1] + '"'
        if template.startswith("host_kube_")
        else "cluster=~" + json.dumps("^$|" + CLUSTERS[cluster][1])
    )
    if not template.startswith("host_kube_"):
        scope += ",instance=~" + json.dumps(device[0] if device else "__no_verified_gpu_host__")
    selector = name + "{" + scope + ("," + filters if filters else "") + "}"
    fresh = f"({selector} and (time()-timestamp({selector})<=120) and (time()-timestamp({selector})>=0))"
    return f"rate({selector}[5m]) and {fresh}" if template.startswith("host_network_") else fresh


def normalize(cluster, at, nodes, aliases, payloads):
    values: dict[str, defaultdict[str, defaultdict[tuple[str, ...], set[float]]]] = {
        n: defaultdict(lambda: defaultdict(set)) for n in nodes
    }
    statuses = {}
    invalid: set[tuple[str, str]] = set()
    for template in SOURCES:
        try:
            payload = payloads.get(template)
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
            statuses[template] = "missing" if not rows else "unmapped"
            for row in rows:
                labels = row.get("metric", {})
                if labels.get("cluster") not in (None, "", CLUSTERS[cluster][1]):
                    continue
                if template.startswith("host_kube_") and labels.get("cluster") != CLUSTERS[cluster][1]:
                    continue
                point = row.get("values", [])
                if len(point) != 1 or point[0][0] != at:
                    raise ValueError()
                v = float(point[0][1])
                if not math.isfinite(v) or v < 0:
                    continue
                candidates = set()
                for k in ("node", "nodename", "instance", "internal_ip"):
                    name = labels.get(k)
                    if name:
                        name = urlsplit("//" + name).hostname if ":" in name else name
                        candidates.update(aliases.get(name, set()))
                if len(candidates) != 1:
                    continue
                node = next(iter(candidates))
                if template in ("host_kube_allocatable_cpu", "host_kube_allocatable_memory"):
                    resource, unit = ("cpu", "core") if template.endswith("cpu") else ("memory", "byte")
                    if labels.get("resource") != resource or labels.get("unit") != unit:
                        invalid.add((node, template))
                        continue
                key: tuple[str, ...] = ()
                if template.startswith("host_filesystem_"):
                    key = tuple(labels.get(k, "") for k in ("device", "mountpoint", "fstype"))
                    if not all(key) or any(len(x) > 512 for x in key):
                        continue
                elif template.startswith("host_network_"):
                    key = (labels.get("device", ""),)
                    if not key[0] or len(key[0]) > 128:
                        continue
                elif template == "host_kube_pressure":
                    key = (labels.get("condition"), labels.get("status"))
                    if (
                        key[0] not in ("MemoryPressure", "DiskPressure", "PIDPressure")
                        or key[1] not in ("true", "false", "unknown")
                        or v not in (0, 1)
                    ):
                        continue
                elif template == "host_kube_unschedulable" and v not in (0, 1):
                    continue
                values[node][template][key].add(v)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            statuses[template] = "error"
    result = {}
    for node in nodes:

        def one(template, key=(), node=node):
            found = values[node][template][key]
            return (
                next(iter(found))
                if len(found) == 1 and statuses[template] != "error" and (node, template) not in invalid
                else None
            )

        disks = []
        keys = set(values[node]["host_filesystem_size"]) | set(values[node]["host_filesystem_available"])
        for key in sorted(keys)[:128]:
            total, available = one("host_filesystem_size", key), one("host_filesystem_available", key)
            if total is not None and (total <= 0 or total > 2**60):
                total = None
            if available is not None and (total is None or available > total):
                available = None
            disks.append(
                dict(zip(("device", "mountpoint", "filesystem"), key, strict=True))
                | {
                    "total_bytes": total,
                    "available_bytes": available,
                    "availability_ratio": available / total
                    if total is not None and available is not None
                    else None,
                }
            )
        networks = []
        nkeys = set(values[node]["host_network_receive"]) | set(values[node]["host_network_transmit"])
        for key in sorted(nkeys)[:64]:
            networks.append(
                {
                    "interface": key[0],
                    "receive_bytes_per_second": one("host_network_receive", key),
                    "transmit_bytes_per_second": one("host_network_transmit", key),
                }
            )
        pressure = {}
        for condition in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            readings = [(s, one("host_kube_pressure", (condition, s))) for s in ("true", "false", "unknown")]
            active = [s for s, v in readings if v == 1]
            pressure[condition] = (
                active[0] if len(active) == 1 and all(v in (0, 1) for _, v in readings) else "unknown"
            )
        metric_statuses = {}
        for t in SOURCES:
            known = [one(t, k) for k in values[node][t]]
            metric_statuses[t] = (
                "ready"
                if known and all(v is not None for v in known)
                else "partial"
                if any(v is not None for v in known)
                else statuses[t]
            )
        for t in SOURCES:
            if (node, t) in invalid and metric_statuses[t] != "error":
                metric_statuses[t] = "unverified"
        for template, validated_readings in (
            ("host_filesystem_size", [d["total_bytes"] for d in disks]),
            ("host_filesystem_available", [d["available_bytes"] for d in disks]),
        ):
            if metric_statuses[template] == "ready" and any(v is None for v in validated_readings):
                metric_statuses[template] = (
                    "partial" if any(v is not None for v in validated_readings) else "unverified"
                )
        if metric_statuses["host_kube_pressure"] == "ready" and any(
            sum(one("host_kube_pressure", (c, s)) == 1 for s in ("true", "false", "unknown")) != 1
            for c in pressure
        ):
            metric_statuses["host_kube_pressure"] = "unverified"
        result[node] = {
            "load1": one("host_load1"),
            "filesystems": disks,
            "filesystems_truncated": len(keys) > 128,
            "networks": networks,
            "networks_truncated": len(nkeys) > 64,
            "kubernetes": {
                "allocatable_cpu_cores": one("host_kube_allocatable_cpu"),
                "allocatable_memory_bytes": one("host_kube_allocatable_memory"),
                "unschedulable": bool(one("host_kube_unschedulable"))
                if one("host_kube_unschedulable") is not None
                else None,
                "pressure": pressure,
            },
            "extended_metric_statuses": metric_statuses,
        }
    return result


def mock(template):
    labels: dict[str, str] = {}
    value = {
        "host_load1": 2.5,
        "host_filesystem_size": 1e12,
        "host_filesystem_available": 6e11,
        "host_network_receive": 2e6,
        "host_network_transmit": 1e6,
        "host_kube_allocatable_cpu": 60,
        "host_kube_allocatable_memory": 250 * 2**30,
        "host_kube_unschedulable": 0,
        "host_kube_pressure": 0,
    }[template]
    if template == "host_kube_allocatable_cpu":
        labels.update(resource="cpu", unit="core")
    if template == "host_kube_allocatable_memory":
        labels.update(resource="memory", unit="byte")
    if template.startswith("host_filesystem_"):
        labels.update(device="/dev/mock0", mountpoint="/", fstype="ext4")
    if template.startswith("host_network_"):
        labels["device"] = "mock-eth0"
    if template == "host_kube_pressure":
        return [
            ({"condition": c, "status": s}, int(s == "false"))
            for c in ("MemoryPressure", "DiskPressure", "PIDPressure")
            for s in ("true", "false", "unknown")
        ]
    return [(labels, value)]
