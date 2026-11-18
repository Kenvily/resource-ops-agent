from typing import Literal

from pydantic import BaseModel, Field

ClusterId = Literal["yz", "zw"]
ClusterFilter = Literal["all", "yz", "zw"]
DataStatus = Literal["healthy", "degraded", "unavailable", "empty", "stale"]


class Metrics(BaseModel):
    gpu_count_observed: int | None = None
    capacity_raw: float | None = None
    allocated_raw: float | None = None
    unallocated_raw: float | None = None
    allocation_ratio: float | None = None
    allocated_observed_raw: float | None = None
    unallocated_observed_raw: float | None = None
    allocation_observed_gpu_count: int | None = None


class WorkloadAllocation(BaseModel):
    cluster: ClusterId
    node: str
    device_uuid: str
    namespace: str
    pod: str
    container: str
    allocated_raw: float | None
    sampled_at: float


class GpuResource(Metrics):
    cluster: ClusterId
    node: str
    device_uuid: str
    sampled_at: float | None
    warnings: list[str] = Field(default_factory=list)


class NodeResource(Metrics):
    cluster: ClusterId
    node: str


class ClusterSummary(Metrics):
    database_read_at: float | None = None
    collection_attempted_at: float | None = None
    collection_succeeded_at: float | None = None
    snapshot_stored_at: float | None = None
    storage_backend: Literal["prometheus", "postgresql"] = "prometheus"
    id: ClusterId
    label: str
    status: DataStatus
    sampled_at: float | None = None
    source_sampled_at: float | None = None
    source_age_seconds: float | None = None
    cache_status: Literal["miss", "hit", "stale"] = "miss"
    cache_age_seconds: float = 0
    fetched_at: float
    warnings: list[str] = Field(default_factory=list)
    gpus: list[GpuResource] = Field(default_factory=list)
    workloads: list[WorkloadAllocation] = Field(default_factory=list)


class Units(BaseModel):
    capacity: str = "unverified"
    allocation: str = "unverified"
    memory: str = "unverified"
    unit_verified: bool = False


class Overview(BaseModel):
    database_read_at: float | None = None
    mode: Literal["mock", "live"]
    overall_status: DataStatus
    fetched_at: float
    units: Units = Field(default_factory=Units)
    complete: bool
    totals: Metrics
    clusters: list[ClusterSummary]
    nodes: list[NodeResource]
    insights: list[str]
