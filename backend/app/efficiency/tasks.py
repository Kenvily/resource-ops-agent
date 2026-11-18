"""Task candidates use verified container measurements, never fractions of host usage."""

from collections import defaultdict
from typing import Any

from app.clients.prometheus import SourceError
from app.efficiency.service import FACTORS
from app.efficiency.store import PROFILE


def task_candidates(request, bundle, approved_models, start, end):
    coverage, devices, workloads, signals = bundle[:4]
    markers = {r[0]: r for r in coverage}
    device_map = {(r[0], r[1], r[2]): r for r in devices}
    parents = {(w[0], w[1], w[2], w[3], w[4], w[5], w[6]): w for w in workloads if w[6] and w[7] > 0}
    samples: dict[tuple, dict[int, Any]] = defaultdict(dict)
    parent_groups: dict[tuple, dict[int, Any]] = defaultdict(dict)
    positive_peers = set()
    for row in signals:
        identity = (row[0], *row[1:7])
        if identity not in parents:
            continue
        key = row[1:7]
        samples[key][row[0]] = row
        if row[11] and row[7] in approved_models and row[8] is not None and row[8] > 0:
            positive_peers.add((row[0], row[1], row[2]))
    for identity, parent in parents.items():
        parent_groups[identity[1:]][identity[0]] = parent
    if len(parent_groups) > 1000:
        raise SourceError("任务候选实体超过1000个，请缩短观察窗口或收窄集群")
    output = defaultdict(list)
    for key, children in parent_groups.items():
        node, uuid, namespace, pod, container, uid = key
        first = min(children)
        expected = (end - first) // 60 + 1
        valid = streak = 0
        previous_restarts = None
        amounts = []
        utils = []
        memories = []
        reason = set()
        for ts in range(first, end + 1, 60):
            w = children.get(ts)
            s = samples[key].get(ts)
            d = device_map.get((ts, node, uuid))
            c = markers.get(ts)
            if not w or not s or not d or not c or not d[8] or c[5] != PROFILE or c[1] != c[2] or not w[11]:
                streak = 0
                previous_restarts = None
                reason.add("缺少同 UID 的连续、可信关联采样")
                continue
            factor = FACTORS.get(d[5])
            trusted = bool(s[11] and s[7] in approved_models and s[8] is not None and factor)
            zero_conflict = (
                s[8] == 0
                and d[6] is not None
                and d[6] > request.utilization_threshold
                and (ts, node, uuid) not in positive_peers
            )
            if not trusted or zero_conflict or s[9] is None or s[10] is None:
                streak = 0
                reason.add("容器信号未验证、缺测或零值与整卡活动冲突")
                continue
            valid += 1
            utils.append(s[8])
            memories.append(s[9])
            amounts.append(w[7] * factor)
            changed = previous_restarts is not None and previous_restarts != w[10]
            eligible = (
                w[9] is True
                and w[8]
                and w[8] <= ts - 900
                and w[7] * factor >= request.minimum_allocation_gb * 1e9
                and s[8] <= request.utilization_threshold
                and s[9] <= request.memory_threshold_mb * 1e6
                and s[10] >= request.sustained_minutes * 60
            )
            streak = (1 if changed else streak + 1) if eligible else 0
            previous_restarts = w[10]
        duration = max(0, streak - 1)
        if valid / expected >= 0.9 and duration >= request.sustained_minutes:
            output[(node, uuid)].append(
                {
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "pod_uid": uid,
                    "scope": "verified_container",
                    "start": first,
                    "end": end,
                    "valid_points": valid,
                    "expected_points": expected,
                    "sustained_minutes": duration,
                    "mean_utilization_pct": sum(utils) / len(utils),
                    "memory_peak_bytes": max(memories),
                    "minimum_allocated_bytes": min(amounts),
                    "requires_human_review": True,
                    "limitations": sorted(reason),
                }
            )
    return output
