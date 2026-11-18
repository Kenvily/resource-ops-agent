"""Optional per-container signals: unknown and unverified zero never mean idle."""

from collections import defaultdict
from typing import Any

from app.clients.prometheus import SourceError
from app.efficiency.normalize import device_id, entries, insert
from app.efficiency.queries import OPTIONAL_METRICS


def normalize_containers(payloads, required, rows, cluster, start, end, settings):
    models: dict[tuple, Any] = {}
    signals: dict[tuple, Any] = {}
    for ts, labels, _value in entries(required["eff_utilization"], cluster, start, end):
        insert(models, (ts, device_id(labels)), labels.get("device_type"))
    failures = []
    for name in OPTIONAL_METRICS:
        payload = payloads.get(name)
        if not payload:
            failures.append(name)
            continue
        try:
            values = list(entries(payload, cluster, start, end))
        except (SourceError, ValueError, TypeError, KeyError, AttributeError):
            failures.append(name)
            continue
        for ts, labels, value in values:
            namespace = (
                labels.get("namespace_name") or labels.get("exported_namespace") or labels.get("namespace")
            )
            pod = labels.get("pod_name") or labels.get("exported_pod") or labels.get("pod")
            container = (
                labels.get("container_name") or labels.get("exported_container") or labels.get("container")
            )
            slot = labels.get("vdevice_index")
            if not all((namespace, pod, container, device_id(labels))) or slot is None:
                continue
            insert(signals, (name, ts, device_id(labels), namespace, pod, container, slot), value)
    grouped = defaultdict(list)
    for key, value in signals.items():
        grouped[key[:-1]].append(value)
    result = []
    for device in rows:
        ts, uuid = device["ts"], device["device_uuid"]
        model = models.get((ts, uuid))
        for w in device["workloads"]:
            identity = (ts, uuid, w["namespace_name"], w["pod_name"], w["container_name"])
            stats = {}
            for name in OPTIONAL_METRICS:
                values = grouped.get((name, *identity), [])
                # Slots are distinct; utilization is a max, memory sums, last activity is the latest kernel.
                stats[name] = (
                    None
                    if not values or any(v is None for v in values)
                    else sum(values)
                    if name == "eff_container_memory"
                    else min(values)
                    if name == "eff_kernel_age"
                    else max(values)
                )
            util = stats["eff_container_utilization"]
            if util is not None and util > 100:
                util = None
            verified = bool(
                model
                and model in settings.efficiency_verified_device_models
                and w["verified"]
                and util is not None
            )
            notes = []
            if not verified:
                notes.append("容器利用率采集能力尚未按设备型号确认，零值不能证明空闲")
            if failures:
                notes.append("部分容器信号缺测，未补零")
            result.append(
                {
                    "ts": ts,
                    "node": device["node"],
                    "device_uuid": uuid,
                    **w,
                    "device_model": model,
                    "utilization_pct": util,
                    "memory_bytes": stats["eff_container_memory"],
                    "kernel_elapsed_seconds": stats["eff_kernel_age"],
                    "utilization_verified": verified,
                    "signal_warnings": notes,
                }
            )
    return result
