import json
from typing import Literal

from app.core.config import CLUSTERS

Template = Literal["capacity", "allocated", "workloads", "freshness"]
RangeTemplate = Literal[
    "wl_pods",
    "wl_containers",
    "wl_running",
    "wl_cpu",
    "wl_memory",
    "wl_requests",
    "wl_limits",
    "host_cpu",
    "host_cpu_count",
    "host_cores",
    "host_memory_total",
    "host_memory_available",
    "host_dmi",
    "host_load1",
    "host_filesystem_size",
    "host_filesystem_available",
    "host_network_receive",
    "host_network_transmit",
    "host_kube_allocatable_cpu",
    "host_kube_allocatable_memory",
    "host_kube_unschedulable",
    "host_kube_pressure",
    "res_capacity_presence",
    "res_gpu_util_sum",
    "res_gpu_util_min",
    "res_gpu_util_max",
    "res_gpu_util_count",
    "res_gpu_memory_sum",
    "res_gpu_memory_min",
    "res_gpu_memory_max",
    "res_gpu_memory_count",
    "res_gpu_memory_free_sum",
    "res_gpu_memory_free_min",
    "res_gpu_memory_free_max",
    "res_gpu_memory_free_count",
    "ops_gpu_util",
    "ops_gpu_memory",
    "ops_gpu_memory_free",
    "ops_gpu_temp",
    "ops_gpu_xid",
    "ops_gpu_ecc",
    "ops_node_ready",
    "ops_node_info",
    "capacity",
    "allocated",
    "device_capacity",
    "device_allocated",
    "workload_allocated",
    "source_age",
    "first_seen",
    "source_probe",
    "eff_capacity",
    "eff_allocated",
    "eff_utilization",
    "eff_memory",
    "eff_pod_info",
    "eff_pod_start",
    "eff_pod_running",
    "eff_pod_restarts",
    "eff_container_utilization",
    "eff_container_memory",
    "eff_kernel_age",
]


def query_for(cluster: str, template: Template) -> str:
    label = CLUSTERS[cluster][1]
    selector = '{cluster="' + label + '"}'
    queries = {
        "capacity": f"max by (cluster,node,device_uuid) (hami_memory_size{selector})",
        "allocated": f"sum by (cluster,node,device_uuid) (hami_container_vmemory_allocated{selector})",
        "workloads": f"hami_container_vmemory_allocated{selector}",
        "freshness": f"time() - min(timestamp(hami_memory_size{selector}) or timestamp(hami_container_vmemory_allocated{selector}))",
    }
    return queries[template]


def range_query_for(
    cluster: str, template: RangeTemplate, absence_zero: bool, device: tuple[str, str] | None = None
) -> str:
    if template.startswith("wl_"):
        from app.workloads.queries import query as workload_query

        return workload_query(cluster, template, device)
    if template.startswith("host_"):
        from app.operations.hosts import host_query

        return host_query(cluster, template, device)
    if template.startswith("res_"):
        from app.resources.queries import resource_query

        return resource_query(cluster, template)
    if template.startswith("ops_"):
        from app.operations.catalog import query

        return query(cluster, template)
    if template.startswith("eff_"):
        from app.efficiency.queries import efficiency_query

        return efficiency_query(cluster, template)
    # Device labels come from a validated current inventory; JSON quoting prevents selector injection.
    selector = "cluster=" + json.dumps(CLUSTERS[cluster][1])
    if device:
        selector += ",node=" + json.dumps(device[0]) + ",device_uuid=" + json.dumps(device[1])
    selector = "{" + selector + "}"
    capacity = f"max by (cluster,node,device_uuid) (hami_memory_size{selector})"
    allocated = f"sum by (cluster,node,device_uuid) (hami_container_vmemory_allocated{selector})"
    if template == "source_probe":
        return f"sum({capacity})"
    if template == "first_seen":
        return f"count(count_over_time(hami_memory_size{selector}[30d]))"
    if template == "source_age":
        return f"time() - min(timestamp(hami_memory_size{selector}) or timestamp(hami_container_vmemory_allocated{selector}))"
    if template == "device_capacity":
        return capacity
    if template == "device_allocated":
        return allocated
    if template == "workload_allocated":
        return f"hami_container_vmemory_allocated{selector}"
    if template == "capacity":
        return f"sum({capacity})"
    if template != "allocated":
        raise KeyError(template)
    effective = f"({allocated} or ({capacity} * 0))" if absence_zero else allocated
    # Do not silently sum partial device coverage or include orphan allocations.
    matched = f"({effective} and {capacity})"
    guard = f"((count({matched}) == count({capacity})) and (count({effective}) == count({capacity})))"
    return f"sum({effective}) and {guard}"
