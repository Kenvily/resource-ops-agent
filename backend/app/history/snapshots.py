"""Database-backed current resources; Prometheus is used only to update persisted snapshots."""

import asyncio
import time

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS
from app.models.resource import ClusterSummary
from app.services.resources import ResourceService


class SnapshotUnavailable(SourceError):
    """No persisted resource result is available."""


class SnapshotService(ResourceService):
    def __init__(self, collector, store):
        super().__init__(collector.client, collector.settings, collector.telemetry)
        self.collector = collector
        self.store = store
        self.sync_status = "idle"

    async def synchronize(self, cluster, force=False, requested_at=None):
        requested_at = time.time() if requested_at is None else requested_at
        async with self.cache.lock((cluster,)), self.store.pool.connection() as conn:
            # Coalesce cross-process updates without waiting through another upstream query.
            locked = await (
                await conn.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)", (7200 if cluster == "yz" else 7201,)
                )
            ).fetchone()
            if not locked[0]:
                return False
            previous = await self.store.snapshot(cluster, conn)
            if previous:
                updated_at = previous[2]
                if (
                    updated_at >= requested_at
                    or time.time() - updated_at < self.settings.snapshot_refresh_min_interval_seconds
                    or (not force and time.time() - updated_at < self.settings.snapshot_sync_interval_seconds)
                ):
                    return True
            value = await self.collector._fetch_cluster(cluster, time.time())
            good = None
            if value.status != "healthy" and previous:
                good = previous[1]
                if good is None and previous[0].get("status") == "healthy":
                    good = previous[0]
            now = time.time()
            if good and now - good["fetched_at"] > self.settings.prom_stale_retention_seconds:
                good = None
            await self.store.save_snapshot(conn, cluster, value.model_dump(mode="json"), good, now)
            return True

    async def cluster(self, cluster, at, refresh=False):
        try:
            synchronized = (
                True if self.settings.production_mode else await self.synchronize(cluster, refresh, at)
            )
            row = await self.store.snapshot(cluster)
            return self.present(cluster, at, row, synchronized)
        except Exception:  # noqa: BLE001 - sanitize database details; never return unpersisted data
            raise SnapshotUnavailable("资源同步或数据库不可用，未返回未经落库的数据") from None

    def present(self, cluster, at, row, synchronized):
        if row is None:
            return ClusterSummary(
                id=cluster,
                label=CLUSTERS[cluster][0],
                status="unavailable",
                fetched_at=at,
                storage_backend="postgresql",
                warnings=["数据库快照正在初始化，请稍后刷新"],
            )
        payload, good, _updated_at = row
        now = time.time()
        value = ClusterSummary.model_validate(payload)
        if (
            value.status == "unavailable"
            and good
            and now - good["fetched_at"] <= self.settings.prom_stale_retention_seconds
        ):
            warnings = value.warnings
            value = ClusterSummary.model_validate(good)
            value.status = "stale"
            value.warnings = warnings + ["采集失败，保留上次成功数据库快照；已标记过期"]
        if synchronized is False and value.status != "unavailable":
            value.status = "stale"
            value.warnings.append("其他进程正在更新快照，当前展示数据库上次结果")
        value.database_read_at = now
        value.collection_attempted_at = payload["fetched_at"]
        value.collection_succeeded_at = (
            payload["fetched_at"] if payload["status"] == "healthy" else good["fetched_at"] if good else None
        )
        value.snapshot_stored_at = _updated_at
        value.storage_backend = "postgresql"
        value.cache_age_seconds = max(0, now - value.fetched_at)
        value.cache_status = "stale" if value.status == "stale" else "hit"
        if value.source_sampled_at is not None:
            value.source_age_seconds = max(0, now - value.source_sampled_at)
        if value.status != "unavailable" and (
            value.cache_age_seconds > self.settings.stale_after_seconds
            or value.source_age_seconds is not None
            and value.source_age_seconds > self.settings.stale_after_seconds
        ):
            value.status = "stale"
            value.cache_status = "stale"
            value.warnings.append("数据库快照或原始采集样本已过期，请核查同步状态")
        return value

    async def update_all(self):
        try:
            await asyncio.gather(*(self.synchronize(cid) for cid in ("yz", "zw")))
            self.sync_status = "ok"
        except Exception:  # noqa: BLE001 - task status excludes private SQL/labels
            self.sync_status = "error"

    async def run(self):
        while True:
            await self.update_all()
            await asyncio.sleep(self.settings.snapshot_sync_interval_seconds)
