import asyncio
import time

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS
from app.core.time_ranges import four_months_ago
from app.history.contracts import validate_scope
from app.history.sync import HistorySync, bounds
from app.models.resource import DataStatus
from app.models.trend import TrendPoint, TrendSeries


class HistoryService:
    """Shared history boundary for Dashboard and future analysis tools."""

    def __init__(self, resources, store):
        self.store = store
        self.sync = HistorySync(resources, store)
        self.resources = resources
        self.gate = asyncio.Semaphore(2)
        from app.history.analysis import HistoryAnalysisService

        self.analysis = HistoryAnalysisService(resources, store)

    async def discovered_earliest(self, cluster):
        """Agent boundary: consume the recorded source start, never discover via Prometheus."""
        async with self.store.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT earliest_ts FROM history_source_state WHERE cluster=%s", (cluster,)
                )
            ).fetchone()
        if not row:
            raise SourceError("源端历史起点尚未记录，请等待后台发现后再查询全部范围")
        return row[0]

    async def earliest(self, cluster):
        if self.resources.settings.production_mode:
            return await self.discovered_earliest(cluster)
        return await self.sync.discover_start(cluster)

    async def discovery_complete(self, cluster):
        async with self.store.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT discovery_complete FROM history_source_state WHERE cluster=%s", (cluster,)
                )
            ).fetchone()
            return bool(row and row[0])

    async def read_committed(self, cluster, grain, start, end, step, dimension="cluster", entity=()):
        """Read-only evidence path: never repairs gaps or calls the upstream source."""
        validate_scope(cluster, grain, dimension, entity)
        now = int(time.time())
        if (
            end < start
            or start < (1 if grain == 3600 else bounds(now, grain)[0])
            or end > now
            or step < grain
            or step % grain
            or start % grain
            or end % grain
            or (end - start) // step + 1 > self.resources.settings.range_max_points
        ):
            raise SourceError("历史证据时间范围或点数无效")
        return await self.store.read_bundle(cluster, grain, start, end, step, dimension, entity)

    async def query(self, cluster, grain, start, end, step, dimension="cluster", entity=()):
        validate_scope(cluster, grain, dimension, entity)
        if grain != 3600 and start < four_months_ago(end):
            raise SourceError("历史查询不能超过四个自然月")
        if end < start or step < grain or step % grain or start % grain or end % grain:
            raise SourceError("历史时间网格无效")
        if (end - start) // step + 1 > self.resources.settings.range_max_points:
            raise SourceError("历史点数超过限制")
        warnings = []
        rows, markers = await self.store.read_bundle(cluster, grain, start, end, step, dimension, entity)
        covered = {row[0] for row in markers if row[3] in ("healthy", "empty")}
        missing = [ts for ts in range(start, end + 1, step) if ts not in covered]
        async with self.gate:
            can_fill = (
                bool(missing)
                and not self.resources.settings.production_mode
                and self.resources.settings.history_query_fill_chunks > 0
            ) and await self.store.claim_fill(
                cluster, grain, self.resources.settings.history_fill_cooldown_seconds
            )
            filled_until = start - 1
            chunks = 0
            for ts in missing if can_fill else []:
                if ts <= filled_until:
                    continue
                if chunks >= self.resources.settings.history_query_fill_chunks:
                    break
                chunks += 1
                filled_until = min(end, ts + (self.resources.settings.history_chunk_points - 1) * grain)
                try:
                    # Read again after waiting: another request may already have filled this point.
                    existing = await self.store.read(cluster, grain, ts, ts, grain)
                    if not existing or existing[0][3] not in ("healthy", "empty"):
                        await self.sync.ingest(cluster, grain, ts, filled_until)
                except SourceError:
                    warnings.append("Prometheus 缺口补查失败，已保留缺测")
        if missing:
            rows, markers = await self.store.read_bundle(cluster, grain, start, end, step, dimension, entity)
            covered = {row[0] for row in markers if row[3] in ("healthy", "empty")}
        if any(ts not in covered for ts in range(start, end + 1, step)):
            warnings.append("历史尚未同步完成，后台将继续回填；未同步点保留缺口")
        for row in markers:
            if row[3] == "degraded":
                warnings.extend(row[4] or ["历史覆盖质量不完整"])
        values = {row[0]: row for row in rows}
        points = []
        for ts in range(start, end + 1, step):
            row = values.get(ts)
            cap, alloc = row[1:3] if row else (None, None)
            if row and row[3] == "degraded":
                warnings.extend(row[4] or ["历史维度数据质量不完整"])
            points.append(
                TrendPoint(
                    timestamp=ts,
                    capacity_raw=cap,
                    allocated_raw=alloc,
                    unallocated_raw=cap - alloc if cap is not None and alloc is not None else None,
                    allocation_ratio=alloc / cap if cap and alloc is not None else None,
                )
            )
        complete = all(
            p.allocated_raw is not None
            and (dimension in ("namespace", "pod", "workload") or p.capacity_raw is not None)
            for p in points
        )
        any_data = any(p.capacity_raw is not None or p.allocated_raw is not None for p in points)
        if any(p.unallocated_raw is not None and p.unallocated_raw < 0 for p in points):
            warnings.append("历史分配超过容量，已保留负未分配额度")
        if any_data and not complete:
            warnings.append(
                "部分时刻无该业务实体记录，未推断为零"
                if dimension in ("namespace", "pod", "workload")
                else "历史采样存在缺口；未补零或跨缺口连线"
            )
        status: DataStatus = (
            "healthy" if complete and not warnings else "degraded" if any_data or warnings else "empty"
        )
        observed = sum(
            p.allocated_raw is not None
            and (dimension in ("namespace", "pod", "workload") or p.capacity_raw is not None)
            for p in points
        )
        stored_times = {r[0] for r in markers}
        collected_at = await self.store.latest_history_collection(cluster, grain, start, end)
        return TrendSeries(
            database_read_at=time.time(),
            history_collected_at=collected_at,
            expected_points=len(points),
            observed_points=observed,
            missing_points=sum(ts not in stored_times for ts in range(start, end + 1, step)),
            invalid_points=sum(r[3] == "degraded" for r in markers),
            id=cluster,
            label=CLUSTERS[cluster][0],
            status=status,
            points=points,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def window_statistics(self, cluster, grain, start, end):
        """Automatic view: exact-window samples first, explicit archive fallback if unavailable."""
        validate_scope(cluster, grain, "cluster", ())
        if end < start or start <= 0 or start % grain or end % grain or end > int(time.time()):
            raise SourceError("统计范围无效")
        expected = (end - start) // grain + 1
        verified = self.resources.units().unit_verified
        try:
            async with self.store.pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        """WITH facts AS (
                    SELECT s.capacity,s.allocated,s.status='healthy' AND c.status='healthy'
                        AND c.last_attempt_status='healthy' AND c.quality_version=2
                        AND c.source_age_seconds IS NOT NULL AND c.source_age_seconds<=%s AS trusted
                    FROM history_entity e JOIN history_sample s ON s.entity_id=e.id
                    JOIN history_coverage c ON (c.cluster,c.grain,c.ts)=(s.cluster,s.grain,s.ts)
                    WHERE e.cluster=%s AND e.dimension='cluster' AND e.entity_key='{}'
                        AND s.grain=%s AND s.ts BETWEEN %s AND %s)
                    SELECT m.name,sum(m.value) FILTER(WHERE f.trusted),
                        min(m.value) FILTER(WHERE f.trusted),max(m.value) FILTER(WHERE f.trusted),
                        count(*) FILTER(WHERE f.trusted AND m.value IS NOT NULL)
                    FROM facts f CROSS JOIN LATERAL (VALUES
                        ('capacity',f.capacity),('allocated',f.allocated),
                        ('unallocated',CASE WHEN %s THEN f.capacity-f.allocated END),
                        ('ratio',CASE WHEN %s AND f.capacity>0 THEN f.allocated/f.capacity END)
                    ) m(name,value) GROUP BY m.name""",
                        (
                            self.resources.settings.stale_after_seconds,
                            cluster,
                            grain,
                            start,
                            end,
                            verified,
                            verified,
                        ),
                    )
                ).fetchall()
        except (DatabaseError, PoolTimeout, OSError):
            raise SourceError("历史统计数据库不可用") from None
        summary = {
            name: {
                "sum": total,
                "mean": total / count if count and total is not None else None,
                "min": lo,
                "max": hi,
                "valid_count": count,
                "coverage": count / expected,
            }
            for name, total, lo, hi, count in rows
        }
        report = {
            "cluster": cluster,
            "source_grain": grain,
            "start": start,
            "end": end,
            "expected_points": expected,
            "expected_buckets": expected,
            "truncated": False,
            "units": self.resources.units().model_dump(),
            "summary": summary,
            "buckets": [],
            "warnings": [],
            "statistics_source": "samples",
        }
        if rows:
            if not summary.get("allocated", {}).get("valid_count", 0):
                report["warnings"].append("当前范围采样质量不足，未使用归档替代异常记录")
            return report
        # Archived grains remain separate; never merge overlapping sampling levels.
        for candidate in (grain, *(g for g in (60, 300, 3600) if g != grain)):
            archived = await self.statistics(cluster, candidate, start, end)
            if archived["summary"].get("allocated", {}).get("valid_count", 0):
                points = (end - start) // candidate + 1
                for value in archived["summary"].values():
                    value["coverage"] = value["valid_count"] / points
                archived.update(
                    expected_points=points,
                    statistics_source="archive",
                    warnings=["当前范围使用已有归档统计，仅包含已关闭的完整时段"],
                )
                return archived
        return report

    async def statistics(self, cluster, grain, start, end, dimension="cluster", entity=()):
        try:
            return await self._statistics(cluster, grain, start, end, dimension, entity)
        except (DatabaseError, PoolTimeout, OSError):
            raise SourceError("历史统计数据库不可用") from None

    async def _statistics(self, cluster, grain, start, end, dimension="cluster", entity=()):
        validate_scope(cluster, grain, dimension, entity)
        if end < start or end > int(time.time()) or start <= 0:
            raise SourceError("统计范围无效")
        width = 86400 if grain == 3600 else 3600
        offset = 28800 if grain == 3600 else 0
        expected_buckets = max(0, (end + offset) // width - (start + offset + width - 1) // width)
        async with self.store.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = await (
                await conn.execute(
                    """SELECT r.bucket,r.bucket_seconds,r.expected_count,
                r.covered_count,r.valid_count,r.metrics,r.finalized FROM history_rollup r
                JOIN history_entity e ON e.id=r.entity_id WHERE e.cluster=%s AND e.dimension=%s
                AND e.entity_key=%s AND r.source_grain=%s AND r.bucket>=%s AND r.bucket+r.bucket_seconds<=%s
                ORDER BY r.bucket LIMIT 1025""",
                    (cluster, dimension, list(entity), grain, start, end),
                )
            ).fetchall()
            summary_rows = await (
                await conn.execute(
                    """SELECT m.key,
                sum((m.value->>'sum')::float8),min((m.value->>'min')::float8),
                max((m.value->>'max')::float8),sum((m.value->>'valid_count')::bigint)::bigint
                FROM history_rollup r JOIN history_entity e ON e.id=r.entity_id
                CROSS JOIN LATERAL jsonb_each(r.metrics) m
                WHERE e.cluster=%s AND e.dimension=%s AND e.entity_key=%s AND r.source_grain=%s
                AND r.bucket>=%s AND r.bucket+r.bucket_seconds<=%s GROUP BY m.key""",
                    (cluster, dimension, list(entity), grain, start, end),
                )
            ).fetchall()
        return {
            "start": start,
            "end": end,
            "dimension": dimension,
            "bucket_seconds": width,
            "summary": {
                key: {
                    "sum": total,
                    "mean": total / n if n and total is not None else None,
                    "min": lo,
                    "max": hi,
                    "valid_count": n,
                    "coverage": n / (expected_buckets * (width // grain)) if expected_buckets else None,
                }
                for key, total, lo, hi, n in summary_rows
            },
            "cluster": cluster,
            "source_grain": grain,
            "units": self.resources.units().model_dump(),
            "truncated": len(rows) > 1024,
            "expected_buckets": expected_buckets,
            "buckets": [
                {
                    "start": r[0],
                    "end": r[0] + r[1],
                    "expected_count": r[2],
                    "covered_count": r[3],
                    "valid_count": r[4],
                    "coverage": r[4] / r[2],
                    "metrics": r[5],
                    "archived": r[6],
                }
                for r in rows[:1024]
            ],
        }

    async def entities(self, cluster, grain, start, end, dimension):
        """Discover historical entities, including those no longer in the current inventory."""
        if (
            grain not in (60, 300, 3600)
            or cluster not in CLUSTERS
            or end < start
            or (grain != 3600 and start < four_months_ago(end))
        ):
            raise SourceError("历史实体查询范围无效")
        rows = await self.store.entities(cluster, grain, start, end, dimension)
        return {"entities": [row[0] for row in rows[:1000]], "truncated": len(rows) > 1000}
