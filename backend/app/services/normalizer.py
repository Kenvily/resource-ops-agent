import math
from collections import defaultdict

from app.core.config import CLUSTERS, Settings
from app.models.resource import (
    ClusterId,
    ClusterSummary,
    DataStatus,
    GpuResource,
    Metrics,
    WorkloadAllocation,
)


def aggregate(items) -> Metrics:
    items = list(items)
    if not items:
        return Metrics()

    def total(field):
        values = [getattr(item, field) for item in items]
        return sum(values) if all(v is not None for v in values) else None

    capacity, allocated = total("capacity_raw"), total("allocated_raw")
    observed_counts = [item.allocation_observed_gpu_count for item in items]
    observed_count = (
        sum(v for v in observed_counts if v is not None)
        if any(v is not None for v in observed_counts)
        else None
    )

    def observed_total(field):
        values = [getattr(item, field) for item in items if getattr(item, field) is not None]
        return sum(values) if values else None

    return Metrics(
        gpu_count_observed=total("gpu_count_observed"),
        allocated_observed_raw=observed_total("allocated_observed_raw"),
        unallocated_observed_raw=observed_total("unallocated_observed_raw"),
        allocation_observed_gpu_count=observed_count,
        capacity_raw=capacity,
        allocated_raw=allocated,
        unallocated_raw=capacity - allocated if capacity is not None and allocated is not None else None,
        allocation_ratio=allocated / capacity if capacity and allocated is not None else None,
    )


def normalize(cluster: ClusterId, payloads: dict, errors: list[str], at: float, settings: Settings):
    warnings = list(errors)
    samples = []

    def parse(name):
        payload = payloads.get(name)
        if payload is None:
            return []
        if payload.get("warnings") or payload.get("infos"):
            warnings.append(f"{name}：数据源返回查询提示，结果可能不完整")
        result = []
        for row in payload["data"]["result"]:
            try:
                metric = row["metric"]
                if not isinstance(metric, dict) or any(
                    not isinstance(metric.get(key), str) or not metric.get(key)
                    for key in ("cluster", "node", "device_uuid")
                ):
                    raise ValueError()
                if (
                    metric.get("cluster") != CLUSTERS[cluster][1]
                    or not metric.get("node")
                    or not metric.get("device_uuid")
                ):
                    raise ValueError()
                timestamp, raw = row["value"]
                timestamp, raw = float(timestamp), float(raw)
                if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > at + 5:
                    raise ValueError()
                if not math.isfinite(raw) or raw < 0:
                    raw = None
                    warnings.append(f"{name}：存在无效数值，已标记缺测")
                samples.append(timestamp)
                result.append((metric, timestamp, raw))
            except (KeyError, TypeError, ValueError):
                warnings.append(f"{name}：存在无效标签或样本")
        return result

    capacities, allocations, workload_rows = parse("capacity"), parse("allocated"), parse("workloads")
    devices: dict[tuple[str, str], GpuResource] = {}
    for labels, timestamp, raw in capacities:
        key = (labels["node"], labels["device_uuid"])
        previous = devices.get(key)
        # PromQL max should already deduplicate; tolerate repeated identical capacity series.
        if previous:
            warnings.append("容量结果存在重复设备，已按最大容量去重")
            if previous.capacity_raw is not None and raw is not None:
                previous.capacity_raw = max(previous.capacity_raw, raw)
            else:
                previous.capacity_raw = None
        else:
            devices[key] = GpuResource(
                cluster=cluster,
                node=key[0],
                device_uuid=key[1],
                sampled_at=timestamp,
                capacity_raw=raw,
                gpu_count_observed=1,
            )
    by_device = defaultdict(list)
    for labels, timestamp, raw in allocations:
        key = (labels["node"], labels["device_uuid"])
        by_device[key].append((timestamp, raw))
        if key not in devices:
            warnings.append("存在分配记录缺少容量指标的设备")
            devices[key] = GpuResource(cluster=cluster, node=key[0], device_uuid=key[1], sampled_at=timestamp)
    for labels, timestamp, _raw in workload_rows:
        key = (labels["node"], labels["device_uuid"])
        if key not in devices:
            warnings.append("业务分配存在未覆盖的 GPU，不能视为零分配")
            devices[key] = GpuResource(cluster=cluster, node=key[0], device_uuid=key[1], sampled_at=timestamp)
    for key, gpu in devices.items():
        values = by_device.get(key, [])
        if len(values) == 1 and values[0][1] is not None:
            if gpu.sampled_at is not None and abs(values[0][0] - gpu.sampled_at) <= 1:
                gpu.allocated_raw = values[0][1]
            else:
                gpu.warnings.append("容量与分配时间未对齐")
        elif len(values) > 1:
            gpu.warnings.append("设备分配汇总重复，需核查 exporter 去重")
        elif "allocated" in payloads and not errors and not warnings and settings.allocation_absence_is_zero:
            gpu.allocated_raw = 0
        else:
            gpu.warnings.append("分配指标缺测，未按零处理")
        if gpu.capacity_raw is not None and gpu.allocated_raw is not None:
            gpu.unallocated_raw = gpu.capacity_raw - gpu.allocated_raw
            gpu.allocation_ratio = gpu.allocated_raw / gpu.capacity_raw if gpu.capacity_raw > 0 else None
            if gpu.unallocated_raw < 0:
                gpu.warnings.append("分配额度超过容量，请核查")
        gpu.allocation_observed_gpu_count = 0
        if gpu.capacity_raw is not None and gpu.allocated_raw is not None:
            gpu.allocated_observed_raw = gpu.allocated_raw
            gpu.unallocated_observed_raw = gpu.unallocated_raw
            gpu.allocation_observed_gpu_count = 1
        warnings.extend(gpu.warnings)
    workloads = []
    for labels, timestamp, raw in workload_rows:
        if any(
            not isinstance(labels.get(k), str) or not labels.get(k)
            for k in ("namespace_name", "pod_name", "container_name")
        ):
            warnings.append("部分业务归属标签缺失，显示未知")
        workloads.append(
            WorkloadAllocation(
                cluster=cluster,
                node=labels["node"],
                device_uuid=labels["device_uuid"],
                namespace=labels["namespace_name"]
                if isinstance(labels.get("namespace_name"), str) and labels["namespace_name"]
                else "未知",
                pod=labels["pod_name"]
                if isinstance(labels.get("pod_name"), str) and labels["pod_name"]
                else "未知",
                container=labels["container_name"]
                if isinstance(labels.get("container_name"), str) and labels["container_name"]
                else "未知",
                allocated_raw=raw,
                sampled_at=timestamp,
            )
        )
    identities = defaultdict(list)
    for row in workloads:
        identities[(row.node, row.device_uuid, row.namespace, row.pod, row.container)].append(row)
    for identity, rows in identities.items():
        if len(rows) > 1:
            warnings.append("业务分配身份重复，无法确认 exporter 副本去重，未累加额度")
            for row in rows:
                row.allocated_raw = None
            gpu = devices[(identity[0], identity[1])]
            gpu.allocated_raw = gpu.unallocated_raw = gpu.allocation_ratio = None
            gpu.allocated_observed_raw = gpu.unallocated_observed_raw = None
            gpu.allocation_observed_gpu_count = 0
            gpu.warnings.append("业务身份重复，设备分配待核验")
    detail_totals: defaultdict[tuple[str, str], float] = defaultdict(float)
    for row in workloads:
        if row.allocated_raw is not None:
            detail_totals[(row.node, row.device_uuid)] += row.allocated_raw
    if "workloads" in payloads:
        for key, gpu in devices.items():
            if gpu.allocated_raw is not None and not math.isclose(detail_totals[key], gpu.allocated_raw):
                warnings.append("容器明细与 GPU 分配汇总不一致，业务排名仅供核查")
                break
    metrics = aggregate(devices.values())
    metrics.gpu_count_observed = (
        len({(labels["node"], labels["device_uuid"]) for labels, _, _ in capacities}) if capacities else None
    )
    source_age = None
    if "freshness" in payloads:
        try:
            source_age = float(payloads["freshness"]["data"]["result"][0]["value"][1])
            if not math.isfinite(source_age) or source_age < 0:
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            source_age = None
            warnings.append("无法确认原始采集时间")
    status: DataStatus = "healthy"
    if not payloads:
        status = "unavailable"
    elif warnings:
        status = "degraded"
    elif not devices:
        status = "empty"
    if (samples and at - min(samples) > settings.stale_after_seconds) or (
        source_age is not None and source_age > settings.stale_after_seconds
    ):
        status = "stale"
        warnings.append("样本已过期，请刷新或核查采集链路")
    return ClusterSummary(
        id=cluster,
        label=CLUSTERS[cluster][0],
        status=status,
        sampled_at=min(samples) if samples else None,
        source_age_seconds=source_age,
        source_sampled_at=at - source_age if source_age is not None else None,
        fetched_at=at,
        warnings=list(dict.fromkeys(warnings)),
        gpus=list(devices.values()),
        workloads=workloads,
        **metrics.model_dump(),
    )
