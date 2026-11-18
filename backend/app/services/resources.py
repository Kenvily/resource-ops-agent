import asyncio
import time
from collections import defaultdict

from app.clients.prometheus import PrometheusClient, SourceError
from app.core.config import Settings
from app.core.telemetry import Telemetry
from app.models.resource import (
    ClusterFilter,
    ClusterId,
    ClusterSummary,
    Metrics,
    NodeResource,
    Overview,
    Units,
)
from app.queries.hami import Template
from app.services.cache import MemoryCache
from app.services.normalizer import aggregate, normalize


class ResourceService:
    def __init__(self, client: PrometheusClient, settings: Settings, telemetry: Telemetry | None = None):
        self.client, self.settings = client, settings
        self.gate = asyncio.Semaphore(2)
        self.telemetry = telemetry or Telemetry()
        self.cache: MemoryCache[ClusterSummary] = MemoryCache(max_entries=2)

    def units(self) -> Units:
        return Units(
            capacity=self.settings.memory_capacity_unit,
            allocation=self.settings.memory_allocation_unit,
            memory=self.settings.memory_capacity_unit
            if self.settings.memory_capacity_unit == self.settings.memory_allocation_unit
            else "unverified",
            unit_verified=self.settings.memory_capacity_unit != "unverified"
            and self.settings.memory_capacity_unit == self.settings.memory_allocation_unit,
        )

    async def _fetch_cluster(self, cluster: ClusterId, at: float):
        names: tuple[Template, ...] = ("capacity", "allocated", "workloads", "freshness")
        results = await asyncio.gather(
            *(self.client.instant_query(cluster, name, at) for name in names), return_exceptions=True
        )
        payloads, errors = {}, []
        for name, result in zip(names, results):
            if isinstance(result, SourceError):
                errors.append(f"{name}：{result}")
            elif isinstance(result, BaseException):
                # A programming error must not masquerade as a healthy source.
                raise result
            else:
                payloads[name] = result
        return normalize(cluster, payloads, errors, at, self.settings)

    async def cluster(self, cluster: ClusterId, at: float, refresh: bool = False):
        key = (cluster,)
        async with self.cache.lock(key):
            cached = self.cache.get(key)
            if cached and (
                (not refresh and cached[0] < self.settings.prom_cache_ttl_seconds)
                or cached[0] < self.settings.snapshot_refresh_min_interval_seconds
            ):
                age, previous = cached
                result = previous.model_copy(deep=True)
                result.cache_status = "hit"
                result.cache_age_seconds = age
                if result.source_age_seconds is not None:
                    result.source_age_seconds += age
                if (
                    result.source_age_seconds is not None
                    and result.source_age_seconds > self.settings.stale_after_seconds
                ):
                    result.status = "stale"
                    result.warnings.append("缓存中的原始采集样本已过期")
                self.telemetry.record("cache", cluster, "overview/hit", 0)
                return result
            self.telemetry.record("cache", cluster, "overview/miss", 0)
            result = await self._fetch_cluster(cluster, at)
            if result.status == "healthy":
                self.cache.put(key, result.model_copy(deep=True))
            elif (
                result.status == "unavailable"
                and cached
                and cached[0] <= self.settings.prom_stale_retention_seconds
            ):
                age, previous = cached
                warnings = result.warnings
                result = previous.model_copy(deep=True)
                result.status = "stale"
                result.cache_status = "stale"
                result.cache_age_seconds = age
                if result.source_age_seconds is not None:
                    result.source_age_seconds += age
                result.warnings = warnings + ["查询失败：保留上次成功缓存，已过期"]
            return result

    async def overview(self, cluster: ClusterFilter = "all", refresh: bool = False):
        async with self.gate:
            at = time.time()
            ids: list[ClusterId] = ["yz", "zw"] if cluster == "all" else [cluster]
            clusters = await asyncio.gather(*(self.cluster(cid, at, refresh) for cid in ids))
        complete = all(c.status == "healthy" for c in clusters)
        groups = defaultdict(list)
        for c in clusters:
            for gpu in c.gpus:
                groups[(c.id, gpu.node)].append(gpu)
        nodes = [
            NodeResource(cluster=cid, node=node, **aggregate(gpus).model_dump())
            for (cid, node), gpus in groups.items()
        ]
        insights = []
        for c in clusters:
            if c.status != "healthy":
                insights.append(
                    f"{c.label}：数据{ {'degraded': '不完整', 'stale': '已过期', 'empty': '为空', 'unavailable': '不可用'}[c.status] }，请先核查数据源。"
                )
            elif c.allocation_ratio is not None:
                insights.append(
                    f"{c.label}虚拟显存分配率 {c.allocation_ratio:.1%}，覆盖 {c.gpu_count_observed} 张 GPU。"
                )
        insights.append("未分配额度不等于物理空闲显存，也不能单独判断 GPU 可调度性。")
        return Overview(
            database_read_at=max((c.database_read_at or 0 for c in clusters), default=0) or None,
            mode=self.settings.data_mode,
            units=self.units(),
            fetched_at=at,
            complete=complete,
            overall_status="healthy"
            if complete
            else ("unavailable" if all(c.status == "unavailable" for c in clusters) else "degraded"),
            # Completeness is per metric: allocation gaps do not invalidate capacity/count.
            # Stale or unavailable sources must never contribute current totals.
            totals=aggregate(c if c.status in ("healthy", "degraded") else Metrics() for c in clusters),
            clusters=clusters,
            nodes=nodes,
            insights=insights,
        )
