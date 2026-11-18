"""Synthetic usage/lifecycle evidence; never used as fallback for Live failures."""

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS


def efficiency_range(fixture, settings, cluster, template, start, end, step):
    if settings.mock_scenario == "partial" and cluster == "zw":
        raise SourceError("演示：使用指标查询超时")
    rows = []
    for index, gpu in enumerate(fixture[cluster]):
        labels = {
            "cluster": CLUSTERS[cluster][1],
            "node": gpu["node"],
            "device_uuid": gpu["uuid"],
            "device_type": "MOCK-NVIDIA",
        }
        low = index % 4 == 0
        if template in ("eff_capacity", "eff_utilization", "eff_memory"):
            value = (
                gpu["capacity"]
                if template == "eff_capacity"
                else (2 if low else 65)
                if template == "eff_utilization"
                else (128e6 if low else 8e9)
            )
            if template == "eff_memory":
                value /= 2**20
            rows.append(
                {
                    "metric": labels,
                    "values": [
                        [ts, str(value)]
                        for ts in range(start, end + 1, step)
                        if not (
                            settings.mock_scenario == "missing"
                            and template == "eff_utilization"
                            and ts // 60 % 7 == 3
                        )
                    ],
                }
            )
        else:
            for allocation in gpu["allocations"]:
                namespace, pod = allocation["namespace"], allocation["pod"]
                if template == "eff_allocated":
                    metric = dict(
                        labels,
                        namespace_name=namespace,
                        pod_name=pod,
                        container_name="worker",
                        namespace="exporter-system",
                        pod="exporter",
                    )
                    value = allocation["value"]
                elif template in ("eff_container_utilization", "eff_container_memory", "eff_kernel_age"):
                    metric = dict(
                        labels,
                        exported_namespace=namespace,
                        exported_pod=pod,
                        container="worker",
                        vdevice_index="0",
                        namespace="exporter-system",
                        pod="exporter",
                    )
                    value = (
                        (2 if low else 65)
                        if template == "eff_container_utilization"
                        else (128e6 if low else 8e9)
                        if template == "eff_container_memory"
                        else (7200 if low else 2)
                    )
                else:
                    metric = dict(
                        labels,
                        namespace=namespace,
                        pod=pod,
                        uid="UID-" + pod,
                        host_ip=gpu["node"],
                        container="worker",
                    )
                    value = (
                        1735689600
                        if template == "eff_pod_start"
                        else 0
                        if template == "eff_pod_restarts"
                        else 1
                    )
                rows.append(
                    {"metric": metric, "values": [[ts, str(value)] for ts in range(start, end + 1, step)]}
                )
    if settings.mock_scenario in ("empty", "stale"):
        rows = []
    return {"status": "success", "data": {"resultType": "matrix", "result": rows}}
