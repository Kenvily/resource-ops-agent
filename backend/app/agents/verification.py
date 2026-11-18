"""Deterministic hypotheses and referenced facts. Model prose is never accepted as a measurement."""

import math
from typing import Any

KINDS = (
    "capacity_change",
    "business_allocation_change",
    "local_history_gap",
    "hourly_sampling_gap",
    "source_data_loss",
    "low_gpu_activity",
)
LABELS = {
    "capacity_change": "监控容量发生变化",
    "business_allocation_change": "业务分配额度发生变化",
    "local_history_gap": "本地区间统计存在覆盖缺口",
    "hourly_sampling_gap": "本地小时采样尚未覆盖完整窗口",
    "source_data_loss": "Prometheus源端历史丢失",
    "low_gpu_activity": "整卡平均计算活动偏低",
}


def assess(kind, evidence, cluster):
    if kind not in KINDS:
        raise ValueError("未知假设")
    rows = [e for e in evidence if e.get("cluster") in (None, cluster) and e.get("status") != "error"]
    support, against = [], []
    for e in rows:
        tool, data = e["tool"], e["data"]
        state = None
        if (
            kind in ("capacity_change", "business_allocation_change")
            and tool == "compare_cluster_windows"
            and e["status"] == "complete"
            and data.get("units", {}).get("unit_verified", False)
        ):
            row: dict[str, Any] = next(iter(data.get("changes", [])), {})
            value = row.get("capacity_change_raw" if kind == "capacity_change" else "allocated_change_raw")
            if isinstance(value, (int, float)) and math.isfinite(value):
                state = abs(value) > 1e-6
        if kind == "local_history_gap" and tool == "check_host_history_coverage":
            state = data["statistics_complete_observed_cohort_timestamps"] < data["expected_grid_points"]
        if kind == "hourly_sampling_gap" and tool == "inspect_host_sync_status":
            # An attempted hour with missing metrics is also a local data gap, not proof of source loss.
            state = data["hourly_point_complete"] < data["hourly_expected"]
        if kind == "low_gpu_activity" and tool == "inspect_gpu_usage":
            value = data.get("mean_utilization_pct")
            if value is not None and data.get("valid_points", 0) > 0:
                state = value <= 10
        if state is True:
            support.append(e["id"])
        elif state is False:
            against.append(e["id"])
    verdict_state = (
        "conflicting"
        if support and against
        else "supported"
        if support
        else "refuted"
        if against
        else "unknown"
    )
    limitations = ["相关变化不能证明因果；分配额度、物理显存与实际利用率分别解释"]
    if kind == "business_allocation_change":
        roots = [e for e in rows if e["tool"] == "compare_cluster_windows" and e["status"] == "complete"]
        ranks = [
            e
            for e in rows
            if e["tool"] == "rank_allocation_changes"
            and e["data"].get("dimension") == "namespace"
            and not e["data"].get("parent_entity_id")
            and e["data"].get("segment", "full") == "full"
        ]
        for root in roots:
            root_row: dict[str, Any] = next(iter(root["data"].get("changes", [])), {})
            delta = root_row.get("allocated_change_raw")
            for rank in ranks:
                cohort = rank["data"].get("cohort_allocated_change_raw")
                if (
                    delta is not None
                    and cohort is not None
                    and not math.isclose(delta, cohort, rel_tol=1e-6, abs_tol=1e-6)
                ):
                    verdict_state = "conflicting"
                    against.append(rank["id"])
                    limitations.append("跨维度贡献不一致，不能完成业务归因")

    if kind == "local_history_gap":
        profiles = {
            (e["data"].get("metric"), e["data"].get("grain"), e["data"].get("start"), e["data"].get("end"))
            for e in rows
            if e["tool"] == "check_host_history_coverage"
        }
        if len(profiles) > 1:
            verdict_state = "unknown"
            limitations.append("指标、窗口或粒度不同，须分组核查，不能把不同口径当成相互反证")
    if kind == "low_gpu_activity":
        entities = {
            (e["data"].get("entity_id"), e["data"].get("segment", "full"))
            for e in rows
            if e["tool"] == "inspect_gpu_usage"
        }
        if len(entities) > 1:
            verdict_state = "unknown"
            limitations.append("涉及不同GPU或子窗口，须逐对象判断")
    if kind == "source_data_loss":
        verdict_state = "unknown"
        limitations = ["尚无源端历史覆盖证据；数据库缺口不能证明Prometheus数据丢失"]
    if kind == "low_gpu_activity":
        limitations = ["平均值偏低不能证明持续低效、任务浪费或额度可回收；须复核尖峰、覆盖及生命周期"]
    return {
        "cluster": cluster,
        "hypothesis": kind,
        "title": LABELS[kind],
        "state": verdict_state,
        "evidence_ids": list(
            dict.fromkeys(
                support
                + against
                + (
                    [
                        e["id"]
                        for e in rows
                        if e["tool"] in ("check_host_history_coverage", "inspect_host_sync_status")
                    ]
                    if kind == "source_data_loss"
                    else []
                )
            )
        ),
        "supporting_evidence_ids": support,
        "counter_evidence_ids": against,
        "limitations": limitations,
    }


def findings(evidence, clusters, metrics=None):
    output = []
    for cluster in clusters:
        for kind in KINDS:
            scoped_evidence = [
                e
                for e in evidence
                if e["tool"] not in ("compare_cluster_windows", "rank_allocation_changes")
                or e["data"].get("segment", "full") == "full"
            ]
            verdict = assess(kind, scoped_evidence, cluster)
            if (
                verdict["state"] != "unknown"
                or kind == "source_data_loss"
                and any(
                    e["tool"] in ("check_host_history_coverage", "inspect_host_sync_status") for e in evidence
                )
            ):
                output.append(
                    {
                        **verdict,
                        "title": {
                            "supported": "已获支持：",
                            "refuted": "已排除：",
                            "conflicting": "证据冲突：",
                            "unknown": "尚无法确认：",
                        }[verdict["state"]]
                        + verdict["title"],
                        "level": "supported_explanation"
                        if verdict["state"] == "supported"
                        else "fact"
                        if verdict["state"] == "refuted"
                        else "unknown",
                    }
                )
        for proof in evidence:
            if proof.get("cluster") != cluster or proof.get("status") == "error":
                continue
            data = proof["data"]
            if proof["tool"] == "get_resource_snapshot":
                s = data["summary"]
                output.append(
                    {
                        "metric": "allocation",
                        "cluster": cluster,
                        "title": f"当前数据库观测GPU {s['gpu_count']}张、服务器 {s['machine_count']}台",
                        "level": "fact" if proof["status"] == "complete" else "unknown",
                        "evidence_ids": [proof["id"]],
                        "limitations": []
                        if proof["status"] == "complete"
                        else ["快照不完整或过期，观测数不能当作完整资产总量"],
                    }
                )
                hosts = data.get("hosts", [])
                count = len(hosts)
                complete_hosts = count == s["machine_count"] and count > 0 and not data.get("hosts_truncated")
                cores_known = complete_hosts and all(h.get("cpu_cores") is not None for h in hosts)
                memory_known = complete_hosts and all(h.get("memory_total_bytes") is not None for h in hosts)

                def inventory_fact(title, known=True, cluster=cluster, proof=proof):
                    output.append(
                        {
                            "metric": "inventory",
                            "cluster": cluster,
                            "title": title,
                            "level": "fact" if known and proof["status"] == "complete" else "unknown",
                            "evidence_ids": [proof["id"]],
                            "limitations": []
                            if known
                            else ["配置缺测或主机覆盖不足，不以部分记录推算完整配置"],
                        }
                    )

                inventory_fact(
                    f"当前数据库观测GPU {s['gpu_count']}张、GPU服务器 {s['machine_count']}台；范围仅限已接入GPU节点"
                )
                inventory_fact(
                    f"CPU逻辑核数合计 {sum(h['cpu_cores'] for h in hosts):g} 核"
                    if cores_known
                    else "CPU逻辑核数合计未知",
                    cores_known,
                )
                inventory_fact(
                    f"系统内存容量合计 {sum(h['memory_total_bytes'] for h in hosts) / 1e9:.2f} GB"
                    if memory_known
                    else "系统内存总容量未知",
                    memory_known,
                )
                from collections import Counter

                models = Counter(h.get("server_model") or "型号未知" for h in hosts)
                inventory_fact(
                    "服务器型号：" + "、".join(f"{name} {n}台" for name, n in models.items())
                    if hosts
                    else "服务器型号未采集",
                    complete_hosts and all(h.get("server_model") for h in hosts),
                )
                gpu_models = Counter(d.get("gpu_model") or "型号未知" for d in data.get("devices", []))
                inventory_fact(
                    "GPU型号：" + "、".join(f"{name} {n}张" for name, n in gpu_models.items())
                    if gpu_models
                    else "GPU型号未采集",
                    bool(gpu_models) and not data.get("devices_truncated"),
                )
                temps = [d.get("temperature_c") for d in data.get("devices", [])]
                temp_known = (
                    bool(temps) and all(v is not None for v in temps) and not data.get("devices_truncated")
                )
                inventory_fact(
                    f"已观测GPU温度范围 {min(temps):g}–{max(temps):g} ℃" if temp_known else "GPU温度覆盖不足",
                    temp_known,
                )
                ready_known = complete_hosts and all(h.get("node_ready") is not None for h in hosts)
                inventory_fact(
                    f"Ready节点 {sum(h['node_ready'] is True for h in hosts)} / {count} 台"
                    if ready_known
                    else "节点Ready状态覆盖不足",
                    ready_known,
                )
                known = data.get("physical_memory_observed_gpus", 0)
                output.append(
                    {
                        "metric": "gpu_memory",
                        "cluster": cluster,
                        "title": f"物理显存为0的GPU {data.get('physical_memory_zero_gpus', 0)} / 已观测 {known} 张"
                        if known
                        else "物理显存观测不足，不能统计零占用卡数",
                        "level": "fact" if known else "unknown",
                        "evidence_ids": [proof["id"]],
                        "limitations": ["零占用仅描述已观测物理显存，不表示虚拟额度未分配或保证可调度"],
                    }
                )
                for device in data.get("devices", []):
                    for metric, key, unit in (
                        ("gpu_util", "utilization_pct", "%"),
                        ("gpu_memory", "physical_used_mib", "MiB"),
                        (
                            "allocation",
                            "allocated_raw",
                            data.get("units", {}).get("allocation", "unverified"),
                        ),
                    ):
                        if device.get(key) is not None:
                            output.append(
                                {
                                    "cluster": cluster,
                                    "entity_ref": device["entity_id"],
                                    "title": metric,
                                    "metric": metric,
                                    "value": device[key],
                                    "unit": unit,
                                    "level": "fact" if unit != "unverified" else "unknown",
                                    "evidence_ids": [proof["id"]],
                                    "limitations": ["已分配为HAMi虚拟显存额度"]
                                    if metric == "allocation"
                                    else [],
                                }
                            )
                for host in data.get("hosts", []):
                    for metric, unit in (
                        ("cpu_utilization_pct", "%"),
                        ("memory_used_bytes", "bytes"),
                        ("load1", "load"),
                    ):
                        if host.get(metric) is not None:
                            output.append(
                                {
                                    "cluster": cluster,
                                    "entity_ref": host["entity_id"],
                                    "title": f"{host['entity_id']} · {metric}: {host[metric]:g} {unit}",
                                    "level": "fact",
                                    "value": host[metric],
                                    "unit": unit,
                                    "metric": metric,
                                    "evidence_ids": [proof["id"]],
                                    "limitations": [],
                                }
                            )
            if proof["tool"] == "get_resource_history":
                output.append(
                    {
                        "cluster": cluster,
                        "title": f"{data['metric']}：有效 {data['observed_points']} / {data['expected_points']} 个趋势点",
                        "level": "fact",
                        "evidence_ids": [proof["id"]],
                        "limitations": [data["limitation"]],
                    }
                )
    return [f for f in output if metrics is None or "metric" not in f or f["metric"] in metrics]
