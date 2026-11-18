import json

from app.core.config import CLUSTERS

SOURCES = {
    "wl_pods": "kube_pod_info",
    "wl_containers": "kube_pod_container_info",
    "wl_running": "kube_pod_status_phase",
    "wl_cpu": "container_cpu_usage_seconds_total",
    "wl_memory": "container_memory_working_set_bytes",
    "wl_requests": "kube_pod_container_resource_requests",
    "wl_limits": "kube_pod_container_resource_limits",
}


def query(cluster, template, device=None):
    scope = "cluster=" + json.dumps(CLUSTERS[cluster][1])
    pattern = device[0] if device else "__no_verified_gpu_node__"
    pods = "kube_pod_info{" + scope + ",node=~" + json.dumps(pattern) + "}"
    fresh_pods = f"({pods} and (time()-timestamp({pods})<=120) and (time()-timestamp({pods})>=0))"
    if template == "wl_pods":
        return fresh_pods
    extra = ',phase="Running"' if template == "wl_running" else ',container!="",container!="POD"'
    if template == "wl_cpu":
        extra += ',cpu=~"total|^$"'
    if template in ("wl_requests", "wl_limits"):
        extra += ',resource=~"cpu|memory",unit=~"core|byte"'
    selector = SOURCES[template] + "{" + scope + extra + "}"
    fresh = f"({selector} and (time()-timestamp({selector})<=120) and (time()-timestamp({selector})>=0))"
    value = f"rate({selector}[5m]) and {fresh}" if template == "wl_cpu" else fresh
    return f"({value}) and on(namespace,pod) {fresh_pods}"
