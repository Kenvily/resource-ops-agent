"""Exact-grid evidence joins; no use of exporter namespace/pod as business ownership."""

import math
from collections import defaultdict
from typing import Any

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS


def device_id(labels):
    ids = {labels[k] for k in ("device_uuid", "deviceuuid", "UUID") if labels.get(k)}
    return next(iter(ids)) if len(ids) == 1 else None


def entries(payload, cluster, start, end):
    if payload.get("warnings") or payload.get("infos"):
        raise SourceError("源端返回部分响应，本批低效核查证据不可用")
    result = payload.get("data", {}).get("result")
    if not isinstance(result, list):
        raise SourceError("使用指标结果格式异常")
    for row in result:
        labels = row.get("metric", {})
        if labels.get("cluster") != CLUSTERS[cluster][1]:
            raise SourceError("使用指标集群标签与请求不一致")
        seen = set()
        for ts, raw in row.get("values", []):
            value = float(raw)
            if ts != int(ts) or ts % 60 or not start <= ts <= end or ts in seen:
                raise SourceError("使用指标时间网格或采样重复异常")
            if not math.isfinite(value) or value < 0:
                raise SourceError("使用指标存在无效数值")
            seen.add(ts)
            yield int(ts), labels, value


def insert(mapping, key, value):
    if key in mapping and mapping[key] != value:
        mapping[key] = None  # Conflicting replicas/identities remain ambiguous, never sum or pick max.
    elif key not in mapping:
        mapping[key] = value


def normalize(payloads, cluster, start, end, settings):
    tables = {name: list(entries(payload, cluster, start, end)) for name, payload in payloads.items()}
    capacities: dict[tuple, Any] = {}
    allocations: dict[tuple, Any] = {}
    hosts: dict[tuple, Any] = {}
    infos: dict[tuple, Any] = {}
    starts: dict[tuple, Any] = {}
    phases: dict[tuple, Any] = {}
    restarts: dict[tuple, Any] = {}
    key: tuple
    for ts, labels, value in tables["eff_capacity"]:
        node, uuid = labels.get("node"), device_id(labels)
        if not node or not uuid:
            raise SourceError("设备容量缺少唯一键")
        insert(capacities, (ts, node, uuid), value)
    for name in ("eff_utilization", "eff_memory"):
        for ts, labels, value in tables[name]:
            uuid = device_id(labels)
            if uuid:
                insert(hosts, (name, ts, uuid), value)
    for ts, labels, value in tables["eff_allocated"]:
        key = (
            ts,
            labels.get("node"),
            device_id(labels),
            labels.get("namespace_name"),
            labels.get("pod_name"),
            labels.get("container_name"),
        )
        if not all(key):
            raise SourceError("额度指标缺少设备或业务身份")
        insert(allocations, key, value)
    for ts, labels, value in tables["eff_pod_info"]:
        if value == 1:
            key = (ts, labels.get("namespace"), labels.get("pod"))
            insert(infos, key, (labels.get("uid"), labels.get("node"), labels.get("host_ip")))
    for name, target in (("eff_pod_start", starts), ("eff_pod_running", phases)):
        for ts, labels, value in tables[name]:
            key = (ts, labels.get("namespace"), labels.get("pod"), labels.get("uid"))
            insert(target, key, value)
    restart_containers: dict[tuple, Any] = {}
    for ts, labels, value in tables["eff_pod_restarts"]:
        key = (
            ts,
            labels.get("namespace"),
            labels.get("pod"),
            labels.get("uid"),
            labels.get("exported_container") or labels.get("container"),
        )
        insert(restart_containers, key, value)
    for key, value in restart_containers.items():
        short = key[:-1]
        if value is None or restarts.get(short, 0) is None:
            restarts[short] = None
        else:
            restarts[short] = restarts.get(short, 0) + value
    workload_groups = defaultdict(list)
    allocation_bad = set()
    for key, amount in allocations.items():
        ts, node, uuid, namespace, pod, container = key
        if amount is None:
            allocation_bad.add((ts, node, uuid))
            continue
        info = infos.get((ts, namespace, pod))
        uid = info[0] if info else None
        identity = (ts, namespace, pod, uid)
        started, running, restart_count = starts.get(identity), phases.get(identity), restarts.get(identity)
        valid = bool(
            info
            and uid
            and node in info[1:]
            and started
            and started <= ts
            and running in (0, 1)
            and restart_count is not None
        )
        workload_groups[(ts, node, uuid)].append(
            {
                "namespace_name": namespace,
                "pod_name": pod,
                "container_name": container,
                "pod_uid": uid,
                "allocated_raw": amount,
                "pod_started_at": started,
                "running": bool(running) if running is not None else None,
                "restart_count": restart_count,
                "verified": valid,
                "warnings": [] if valid else ["Pod UID、节点或生命周期关联证据不足"],
            }
        )
    uuid_nodes = defaultdict(set)
    for ts, node, uuid in capacities:
        uuid_nodes[(ts, uuid)].add(node)
    rows = []
    for (ts, node, uuid), capacity in capacities.items():
        warnings = []
        utilization = hosts.get(("eff_utilization", ts, uuid))
        memory = hosts.get(("eff_memory", ts, uuid))
        if memory is not None and not 0 <= memory <= 2**40:
            memory = None
        if memory is not None:
            memory *= 2**20  # DCGM FB_USED is MiB; legacy evidence contract remains bytes.
        if utilization is not None and utilization > 100:
            utilization = None
        if len(uuid_nodes[(ts, uuid)]) != 1:
            utilization = memory = None
            warnings.append("同一时刻 UUID 对应多个节点，未关联实际使用指标")
        workloads = workload_groups[(ts, node, uuid)]
        amount = sum(w["allocated_raw"] for w in workloads)
        if not workloads and not settings.allocation_absence_is_zero or (ts, node, uuid) in allocation_bad:
            amount = None
        if utilization is None or memory is None:
            warnings.append("实际整卡使用指标缺测或冲突")
        if capacity is None or capacity <= 0 or amount is None:
            warnings.append("额度或容量证据不足")
        if not all(w["verified"] for w in workloads):
            warnings.append("业务生命周期证据不足")
        rows.append(
            {
                "ts": ts,
                "node": node,
                "device_uuid": uuid,
                "capacity_raw": capacity,
                "allocated_raw": amount,
                "allocation_unit": settings.memory_allocation_unit,
                "utilization_pct": utilization,
                "physical_memory_bytes": memory,
                "verified": not warnings,
                "warnings": warnings,
                "workloads": workloads,
            }
        )
    # Allocation devices missing from capacity are not silently ignored as a complete cohort.
    if any(key[:3] not in capacities for key in allocations):
        raise SourceError("存在无法关联容量的额度记录，本批未用于低效核查")
    return rows
