"""Fixed DCGM host, HAMi allocation and kube-state-metrics templates. No caller-defined expressions."""

import json

from app.core.config import CLUSTERS

METRICS = {
    "eff_capacity": "hami_memory_size",
    "eff_allocated": "hami_container_vmemory_allocated",
    "eff_utilization": "DCGM_FI_DEV_GPU_UTIL",
    "eff_memory": "DCGM_FI_DEV_FB_USED",
    "eff_pod_info": "kube_pod_info",
    "eff_pod_start": "kube_pod_start_time",
    "eff_pod_running": "kube_pod_status_phase",
    "eff_pod_restarts": "kube_pod_container_status_restarts_total",
}


OPTIONAL_METRICS = {
    "eff_container_utilization": "hami_container_device_utilization_ratio",
    "eff_container_memory": "hami_container_device_memory_bytes",
    "eff_kernel_age": "hami_container_last_kernel_elapsed_seconds",
}


def efficiency_query(cluster, template):
    selector = "{cluster=" + json.dumps(CLUSTERS[cluster][1]) + "}"
    metric = (METRICS | OPTIONAL_METRICS)[template] + selector
    if template == "eff_pod_running":
        metric = METRICS[template] + "{cluster=" + json.dumps(CLUSTERS[cluster][1]) + ',phase="Running"}'
    # Keep original labels and values, reject stale source samples before range evaluation.
    fresh = f"({metric} and (time() - timestamp({metric}) <= 120) and (time() - timestamp({metric}) >= 0))"
    if template.startswith("eff_pod_"):
        allocation = f"hami_container_vmemory_allocated{selector}"
        names = f'label_replace(label_replace({allocation}, "namespace", "$1", "namespace_name", "(.+)"), "pod", "$1", "pod_name", "(.+)")'
        # Restrict KSM to allocated workloads before transferring any data.
        fresh += f" and on(namespace,pod) ({names})"
    return fresh
