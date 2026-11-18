import asyncio
import math
import time

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS
from app.core.time_ranges import four_months_ago, range_seconds
from app.models.resource import ClusterFilter, ClusterId, DataStatus
from app.models.trend import TrendPoint, TrendResponse, TrendSeries, Window
from app.services.cache import MemoryCache
from app.services.resources import ResourceService

WINDOW_SECONDS = {"1h": 3600, "24h": 86400, "7d": 604800}


def parse_matrix(payload: dict, start: int, end: int, step: int) -> tuple[dict[int, float | None], list[str]]:
    warnings: list[str] = []
    result = payload["data"]["result"]
    if payload.get("warnings") or payload.get("infos"):
        warnings.append("数据源返回历史查询提示，结果可能不完整")
    if not result:
        return {}, warnings
    if len(result) != 1:
        raise SourceError("历史聚合返回多条序列")
    values: dict[int, float | None] = {}
    for pair in result[0]["values"]:
        try:
            timestamp, raw = float(pair[0]), float(pair[1])
            if not math.isfinite(timestamp) or timestamp != int(timestamp):
                raise ValueError()
            point = int(timestamp)
            if point < start or point > end or (point - start) % step or point in values:
                raise ValueError()
            if not math.isfinite(raw) or raw < 0:
                values[point] = None
                warnings.append("历史存在无效数值，保留为缺口")
            else:
                values[point] = raw
        except (ValueError, IndexError, TypeError, OverflowError):
            warnings.append("历史存在无效或未对齐的采样点")
    return values, list(dict.fromkeys(warnings))


class TrendService:
    def __init__(self, resources: ResourceService, history=None):
        self.history = history
        self.resources = resources
        self.settings = resources.settings
        self.cache: MemoryCache[TrendResponse] = MemoryCache(max_entries=64)
        self.gate = asyncio.Semaphore(2)

    async def load_range(self, cluster: ClusterId, template, start: int, end: int, step: int, device):
        # Long queries can produce partial responses at an older store boundary.
        # Use disjoint, grid-aligned <=30-day segments; refine partial segments only.
        if end - start <= 30 * 86400:
            return await self.resources.client.range_query(cluster, template, start, end, step, device)
        attempts = 0

        async def segment(left: int, right: int, depth: int = 0):
            nonlocal attempts
            if attempts >= 32:
                raise SourceError("历史分段查询达到重试上限，部分数据不可用")
            attempts += 1
            payload = await self.resources.client.range_query(cluster, template, left, right, step, device)
            count = sum(len(row["values"]) for row in payload["data"]["result"])
            expected = (right - left) // step + 1
            if payload.get("warnings") and count < expected and right - left > 86400 and depth < 5:
                middle = left + ((right - left) // step // 2) * step
                children = await asyncio.gather(
                    segment(left, middle, depth + 1),
                    segment(middle + step, right, depth + 1),
                    return_exceptions=True,
                )
                return combine(children)
            return payload

        def combine(results):
            values: list = []
            warnings: list[str] = []
            errors: list[str] = []
            for result in results:
                if isinstance(result, SourceError):
                    errors.append(str(result))
                    warnings.append("历史分段查询部分失败")
                elif isinstance(result, BaseException):
                    raise result
                else:
                    values.extend(value for row in result["data"]["result"] for value in row["values"])
                    warnings.extend(result.get("warnings", []))
                    warnings.extend(result.get("infos", []))
            if not values and errors:
                raise SourceError(errors[0])
            return {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {}, "values": values}] if values else [],
                },
                "warnings": warnings,
            }

        width = max(1, (30 * 86400) // step + 1)
        count = (end - start) // step + 1
        tasks = [
            segment(start + index * step, start + min(count - 1, index + width - 1) * step)
            for index in range(0, count, width)
        ]
        try:
            async with asyncio.timeout(25):
                return combine(await asyncio.gather(*tasks, return_exceptions=True))
        except TimeoutError:
            raise SourceError("历史分段查询总超时，请重试或缩短窗口") from None

    async def series(
        self,
        cluster: ClusterId,
        start: int,
        end: int,
        step: int,
        device: tuple[str, str] | None,
        source_start_at=None,
    ):
        if self.history:
            grain = 3600 if step % 3600 == 0 else 300 if step % 300 == 0 else 60
            try:
                active_start = (
                    start
                    if source_start_at is None
                    else start + max(0, math.ceil((source_start_at - start) / step)) * step
                )
                if active_start > end:
                    return TrendSeries(
                        id=cluster,
                        label=CLUSTERS[cluster][0],
                        status="empty",
                        source_start_at=source_start_at,
                        points=[TrendPoint(timestamp=t) for t in range(start, end + 1, step)],
                    )
                result = await self.history.query(
                    cluster, grain, active_start, end, step, "gpu" if device else "cluster", device or ()
                )
                result.source_start_at = source_start_at
                if active_start > start:
                    result.points = [
                        TrendPoint(timestamp=t) for t in range(start, active_start, step)
                    ] + result.points
                return result
            except Exception:  # noqa: BLE001 - isolated boundary; never log private exception details
                return TrendSeries(
                    id=cluster,
                    label=CLUSTERS[cluster][0],
                    status="unavailable",
                    points=[TrendPoint(timestamp=ts) for ts in range(start, end + 1, step)],
                    warnings=["历史数据库暂时不可用；未使用其他来源替代数据库结果"],
                )
        return await self._prometheus_series(cluster, start, end, step, device)

    async def _prometheus_series(self, cluster, start, end, step, device):
        results = await asyncio.gather(
            *(
                self.load_range(cluster, template, start, end, step, device)
                for template in ("capacity", "allocated")
            ),
            return_exceptions=True,
        )
        datasets: list[dict[int, float | None]] = []
        warnings = []
        failures = 0
        for result in results:
            if isinstance(result, SourceError):
                datasets.append({})
                warnings.append(str(result))
                failures += 1
            elif isinstance(result, BaseException):
                raise result
            else:
                parsed, notes = parse_matrix(result, start, end, step)
                datasets.append(parsed)
                warnings.extend(notes)
        capacity, allocated = datasets
        points = []
        for timestamp in range(start, end + 1, step):
            total, amount = capacity.get(timestamp), allocated.get(timestamp)
            remaining = total - amount if total is not None and amount is not None else None
            if remaining is not None and remaining < 0:
                warnings.append("历史分配超过容量，已保留负未分配额度")
            points.append(
                TrendPoint(
                    timestamp=timestamp,
                    capacity_raw=total,
                    allocated_raw=amount,
                    unallocated_raw=remaining,
                    allocation_ratio=amount / total if total and amount is not None else None,
                )
            )
        any_data = any(p.capacity_raw is not None or p.allocated_raw is not None for p in points)
        complete = all(p.capacity_raw is not None and p.allocated_raw is not None for p in points)
        if any_data and not complete:
            warnings.append("历史采样存在缺口；未补零或跨缺口连线")
        status: DataStatus = (
            "unavailable"
            if failures == 2
            else "empty"
            if not any_data and not failures
            else ("healthy" if complete and not warnings else "degraded")
        )
        return TrendSeries(
            id=cluster,
            label=CLUSTERS[cluster][0],
            status=status,
            points=points,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def query(
        self,
        cluster: ClusterFilter,
        window: Window,
        device: tuple[str, str] | None = None,
        refresh: bool = False,
    ) -> TrendResponse:
        if device:
            snapshot = await self.resources.overview(cluster)
            if not any(
                g.node == device[0] and g.device_uuid == device[1] for c in snapshot.clusters for g in c.gpus
            ):
                raise SourceError("未找到该设备，无法查询历史")
        now = int(time.time())
        discovered_start = None
        source_starts = {}
        complete_starts = {}
        if window in ("1mo", "3mo", "all", "4mo"):
            # Calendar months, not a fixed 120-day approximation. Keep the latest minute.
            end = now // 60 * 60
            seconds = range_seconds(window, end)
            if window == "all":
                if not self.history and self.settings.data_mode != "mock":
                    raise SourceError("全部历史需配置数据库以记录源端起点")
                discovery_ids = ["yz", "zw"] if cluster == "all" else [cluster]
                if self.history:
                    starts = await asyncio.gather(*(self.history.earliest(c) for c in discovery_ids))
                    source_starts = dict(zip(discovery_ids, starts))
                    discovered_start = min(starts)
                    verified = await asyncio.gather(
                        *(self.history.discovery_complete(c) for c in discovery_ids)
                    )
                    complete_starts = {c: t for c, t, valid in zip(discovery_ids, starts, verified) if valid}
                else:
                    discovered_start = four_months_ago(end)
                seconds = end - discovered_start
            step = max(60, math.ceil(seconds / (self.settings.range_max_points - 1) / 60) * 60)
        else:
            seconds = range_seconds(window, now)
            step = max(60, math.ceil(seconds / (self.settings.range_max_points - 1) / 60) * 60)
            end = now // step * step
        if self.history:
            from app.history.sync import grain_for

            grain = grain_for(window)
            end = (now - 120) // grain * grain
            seconds = (
                end - discovered_start
                if window == "all" and discovered_start is not None
                else range_seconds(window, end)
            )
            step = max(grain, math.ceil(seconds / (self.settings.range_max_points - 1) / grain) * grain)
        # Include the latest point; shorten by less than one step instead of exceeding the boundary.
        start = end - (seconds // step) * step
        key = (cluster, window, device, end)
        async with self.gate, self.cache.lock(key):
            cached = self.cache.get(key)
            if (
                not self.history
                and cached
                and (
                    (not refresh and cached[0] < self.settings.range_cache_ttl_seconds)
                    or cached[0] < self.settings.snapshot_refresh_min_interval_seconds
                )
            ):
                age, previous = cached
                response = previous.model_copy(deep=True)
                response.cache_status = "hit"
                response.cache_age_seconds = age
                self.resources.telemetry.record("cache", cluster, "trends/hit", 0)
                return response
            ids: list[ClusterId] = ["yz", "zw"] if cluster == "all" else [cluster]
            series = await asyncio.gather(
                *(self.series(cid, start, end, step, device, complete_starts.get(cid)) for cid in ids)
            )
            if window == "all" and self.history:
                for item in series:
                    item.source_start_at = source_starts.get(item.id)
                    if not await self.history.discovery_complete(item.id):
                        item.status = "degraded"
                        item.warnings.append(
                            "源端起点探测返回部分结果；当前从已发现最早时间展示，可能存在更早数据"
                        )
            response = TrendResponse(
                mode=self.settings.data_mode,
                units=self.resources.units(),
                window=window,
                start=start,
                end=end,
                step=step,
                fetched_at=time.time(),
                complete=all(s.status == "healthy" for s in series),
                series=series,
            )
            # Failed/partial results are retried rather than cached as healthy history.
            if response.complete and not self.history:
                self.cache.put(key, response.model_copy(deep=True))
            self.resources.telemetry.record("cache", cluster, "trends/miss", 0)
            return response
