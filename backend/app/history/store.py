import logging
import time
from pathlib import Path

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.clients.prometheus import SourceError
from app.history.contracts import ARITY, QUALITY_VERSION, SCHEMA_VERSION, validate_scope
from app.history.rollups import aggregate


class SafePoolLog(logging.Filter):
    def filter(self, record):
        record.msg = "历史数据库连接池状态异常，请检查数据库与配置"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


class HistoryStore:
    def __init__(self, settings, *, read_only=False):
        self.settings = settings
        self.read_only = read_only
        logger = logging.getLogger("psycopg.pool")
        if not any(isinstance(f, SafePoolLog) for f in logger.filters):
            logger.addFilter(SafePoolLog())
        self.pool = AsyncConnectionPool(
            settings.history_database_url.get_secret_value(),
            min_size=1,
            max_size=4,
            open=False,
            timeout=3,
            kwargs={
                "connect_timeout": 3,
                "options": "-c statement_timeout=5000"
                + (" -c default_transaction_read_only=on" if read_only else ""),
            },
        )

    def contract(self):
        return {
            "mode": self.settings.data_mode,
            "capacity_unit": self.settings.memory_capacity_unit,
            "allocation_unit": self.settings.memory_allocation_unit,
            "absence_zero": self.settings.allocation_absence_is_zero,
            "origins": [self.settings.prom_yz_url, self.settings.prom_zw_url],
        }

    async def open(self):
        await self.pool.open(wait=True, timeout=5)

    async def close(self):
        await self.pool.close()

    async def initialize(self):
        if self.read_only:
            raise SourceError("只读验收连接不能执行数据库初始化")
        # Administrative, transactional migrations. Reject foreign contracts BEFORE DDL.
        async with self.pool.connection() as conn:
            await conn.execute("SET LOCAL statement_timeout=120000")
            await conn.execute("SELECT pg_advisory_xact_lock(710000)")
            exists = await (await conn.execute("SELECT to_regclass('history_metadata')")).fetchone()
            root = Path(__file__).parent
            version = 0
            if exists and exists[0]:
                row = await (
                    await conn.execute("SELECT version,contract FROM history_metadata WHERE id=1")
                ).fetchone()
                if (
                    not row
                    or row[1] != self.contract()
                    or row[0] not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
                ):
                    raise SourceError("历史数据库版本或数据契约不匹配，未执行迁移")
                version = row[0]
            if version < 2:
                await conn.execute((root / "schema.sql").read_text())
                await conn.execute(
                    "INSERT INTO history_metadata VALUES (1,2,%s) ON CONFLICT (id) DO UPDATE SET version=2",
                    (Jsonb(self.contract()),),
                )
                version = 2
            if version == 2:
                await conn.execute((root / "migrations/003_entities_coverage.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=3 WHERE id=1")
                version = 3
            if version == 3:
                await conn.execute((root / "migrations/004_rollups.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=4 WHERE id=1")
                version = 4
            if version == 4:
                await conn.execute((root / "migrations/005_efficiency.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=5 WHERE id=1")
                version = 5
            if version == 5:
                await conn.execute((root / "migrations/006_efficiency_agent.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=6 WHERE id=1")
                version = 6
            if version == 6:
                await conn.execute((root / "migrations/007_operations.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=7 WHERE id=1")
                version = 7
            if version == 7:
                await conn.execute((root / "migrations/008_resource_statistics.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=8 WHERE id=1")
                version = 8
            if version == 8:
                await conn.execute((root / "migrations/009_hosts_investigations.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=9 WHERE id=1")
                version = 9
            if version == 9:
                await conn.execute((root / "migrations/010_operations_workspace.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=10 WHERE id=1")
                version = 10
            if version == 10:
                await conn.execute((root / "migrations/011_host_history.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=11 WHERE id=1")
                version = 11
            if version == 11:
                await conn.execute((root / "migrations/012_workload_resources.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=12 WHERE id=1")
                version = 12
            if version == 12:
                await conn.execute((root / "migrations/013_host_points.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=13 WHERE id=1")
                version = 13
            if version == 13:
                await conn.execute((root / "migrations/014_agent_memory.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=14 WHERE id=1")
                version = 14
            if version == 14:
                await conn.execute((root / "migrations/015_product_analytics.sql").read_text())
                await conn.execute("UPDATE history_metadata SET version=15 WHERE id=1")
        await self.validate()

    async def validate(self):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT version,contract FROM history_metadata WHERE id=1")
            ).fetchone()
        if row != (SCHEMA_VERSION, self.contract()):
            raise SourceError("历史数据库版本、数据源或单位配置不匹配，请执行迁移或使用独立数据库")

    async def claim_fill(self, cluster, grain, cooldown):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """INSERT INTO history_fill_guard(cluster,grain) VALUES(%s,%s)
                ON CONFLICT(cluster,grain) DO UPDATE SET attempted_at=now()
                WHERE history_fill_guard.attempted_at<=now()-(%s*interval '1 second') RETURNING cluster""",
                    (cluster, grain, cooldown),
                )
            ).fetchone()
            return bool(row)

    async def earliest(self, cluster):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT earliest_ts FROM history_source_state WHERE cluster=%s", (cluster,)
                )
            ).fetchone()
            return row[0] if row else None

    async def earliest_available(self, cluster):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT min(ts) FROM history_sample WHERE cluster=%s AND grain=3600 AND dimension='cluster' AND capacity>0",
                    (cluster,),
                )
            ).fetchone()
            return row[0] if row and row[0] else None

    async def save_earliest(self, cluster, ts, complete=True):
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO history_source_state(cluster,earliest_ts,discovery_complete) VALUES(%s,%s,%s)
                ON CONFLICT(cluster) DO UPDATE SET earliest_ts=least(history_source_state.earliest_ts,excluded.earliest_ts),discovery_complete=excluded.discovery_complete,discovered_at=now()""",
                (cluster, ts, complete),
            )

    async def read(self, cluster, grain, start, end, step, dimension="cluster", entity=()):
        validate_scope(cluster, grain, dimension, entity)
        async with self.pool.connection() as conn:
            return await self._read(conn, cluster, grain, start, end, step, dimension, entity)

    async def _read(self, conn, cluster, grain, start, end, step, dimension, entity):
        return await (
            await conn.execute(
                """
            SELECT s.ts,s.capacity,s.allocated,
            CASE WHEN c.quality_version<2 OR (c.status='healthy' AND (c.last_attempt_status<>'healthy' OR c.source_age_seconds IS NULL OR c.source_age_seconds>%s)) THEN 'degraded' ELSE s.status END,
            s.warnings || c.warnings || c.last_attempt_warnings ||
                CASE WHEN c.status='healthy' AND (c.source_age_seconds IS NULL OR c.source_age_seconds>%s)
                    THEN '["历史采集时间未确认或已过期"]'::jsonb ELSE '[]'::jsonb END ||
                CASE WHEN c.quality_version<2 THEN '["旧历史口径待重新核验"]'::jsonb ELSE '[]'::jsonb END ||
                CASE WHEN c.last_attempt_status<>'healthy' AND c.status='healthy'
                    THEN '["最新复查不完整，保留已验证历史"]'::jsonb ELSE '[]'::jsonb END
            FROM history_entity e JOIN history_sample s ON s.entity_id=e.id
            JOIN history_coverage c ON (c.cluster,c.grain,c.ts)=(s.cluster,s.grain,s.ts)
            WHERE e.cluster=%s AND e.dimension=%s AND e.entity_key=%s AND s.grain=%s
            AND s.ts BETWEEN %s AND %s AND (s.ts-%s) %% %s=0 ORDER BY s.ts""",
                (
                    self.settings.stale_after_seconds,
                    self.settings.stale_after_seconds,
                    cluster,
                    dimension,
                    list(entity),
                    grain,
                    start,
                    end,
                    start,
                    step,
                ),
            )
        ).fetchall()

    async def latest_history_collection(self, cluster, grain, start, end):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT max(last_attempt_at) FROM history_coverage WHERE cluster=%s AND grain=%s AND ts BETWEEN %s AND %s",
                    (cluster, grain, start, end),
                )
            ).fetchone()
            return row[0].timestamp() if row and row[0] else None

    async def read_bundle(self, cluster, grain, start, end, step, dimension="cluster", entity=()):
        validate_scope(cluster, grain, dimension, entity)
        # Both reads share an MVCC snapshot, including concurrent replacement/pruning.
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = await self._read(conn, cluster, grain, start, end, step, dimension, entity)
            if dimension == "cluster":
                return rows, rows
            markers = await self._read(conn, cluster, grain, start, end, step, "cluster", ())
            return rows, markers

    async def write(self, cluster, grain, start, end, rows, checkpoint=False, source_ages=None):
        validate_scope(cluster, grain, "cluster", ())
        if start <= 0 or end < start or start % grain or end % grain:
            raise SourceError("历史写入时间网格无效")
        source_ages = source_ages or {}
        headers = {}
        for ts, dim, key, cap, alloc, status, notes in rows:
            validate_scope(cluster, grain, dim, key)
            if ts < start or ts > end or ts % grain:
                raise SourceError("历史样本超出写入网格")
            if dim == "cluster":
                if ts in headers:
                    raise SourceError("历史覆盖标记重复")
                headers[ts] = (status, notes)
        if set(headers) != set(range(start, end + 1, grain)):
            raise SourceError("历史写入缺少覆盖标记")
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (7100 if cluster == "yz" else 7101,))
            old = await (
                await conn.execute(
                    "SELECT ts,status,quality_version FROM history_coverage WHERE cluster=%s AND grain=%s AND ts BETWEEN %s AND %s",
                    (cluster, grain, start, end),
                )
            ).fetchall()
            protected = {
                ts
                for ts, status, version in old
                if {"healthy": 2, "degraded": 1, "empty": 0}[status]
                > {"healthy": 2, "degraded": 1, "empty": 0}[headers[ts][0]]
            }
            async with conn.cursor() as cur:
                for ts in protected:
                    await cur.execute(
                        "UPDATE history_coverage SET last_attempt_status=%s,last_attempt_at=now(),last_attempt_warnings=%s WHERE cluster=%s AND grain=%s AND ts=%s",
                        (headers[ts][0], Jsonb(headers[ts][1]), cluster, grain, ts),
                    )
                accepted = [ts for ts in headers if ts not in protected]
                if accepted:
                    await cur.executemany(
                        """INSERT INTO history_coverage
                        (cluster,grain,ts,status,warnings,quality_version,last_attempt_status,source_age_seconds,last_attempt_warnings)
                        VALUES (%s,%s,%s,%s,%s,2,%s,%s,%s) ON CONFLICT (cluster,grain,ts) DO UPDATE
                        SET status=excluded.status,warnings=excluded.warnings,quality_version=2,
                        last_attempt_status=excluded.last_attempt_status,last_attempt_at=now(),
                        source_age_seconds=excluded.source_age_seconds,last_attempt_warnings=excluded.last_attempt_warnings""",
                        [
                            (
                                cluster,
                                grain,
                                ts,
                                headers[ts][0],
                                Jsonb(headers[ts][1]),
                                headers[ts][0],
                                source_ages.get(ts),
                                Jsonb(headers[ts][1]),
                            )
                            for ts in accepted
                        ],
                    )
                    await cur.execute(
                        "DELETE FROM history_sample WHERE cluster=%s AND grain=%s AND ts=ANY(%s)",
                        (cluster, grain, accepted),
                    )
                    selected = [r for r in rows if r[0] not in protected]
                    keys = [
                        {"dimension": d, "entity_key": list(k)}
                        for d, k in sorted({(r[1], tuple(r[2])) for r in selected})
                    ]
                    await cur.execute(
                        """INSERT INTO history_entity (cluster,dimension,entity_key)
                        SELECT %s,x.dimension,x.entity_key FROM jsonb_to_recordset(%s) x(dimension text,entity_key text[])
                        ON CONFLICT (cluster,dimension,entity_key) DO NOTHING""",
                        (cluster, Jsonb(keys)),
                    )
                    ids = await (
                        await conn.execute(
                            """SELECT e.id,e.dimension,e.entity_key FROM history_entity e
                        JOIN jsonb_to_recordset(%s) x(dimension text,entity_key text[])
                        ON e.dimension=x.dimension AND e.entity_key=x.entity_key WHERE e.cluster=%s""",
                            (Jsonb(keys), cluster),
                        )
                    ).fetchall()
                    mapping = {(d, tuple(k)): eid for eid, d, k in ids}
                    await cur.executemany(
                        """INSERT INTO history_sample
                        (cluster,grain,ts,dimension,capacity,allocated,status,warnings,entity_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [
                            (cluster, grain, ts, d, cap, alloc, status, Jsonb([]), mapping[d, tuple(key)])
                            for ts, d, key, cap, alloc, status, notes in selected
                        ],
                    )
            await aggregate(
                conn,
                cluster,
                grain,
                start,
                end,
                int(time.time()),
                self.settings.stale_after_seconds,
                self.settings.memory_capacity_unit != "unverified"
                and self.settings.memory_capacity_unit == self.settings.memory_allocation_unit,
            )
            if checkpoint:
                await conn.execute(
                    """INSERT INTO history_progress (cluster,grain,next_ts,quality_version) VALUES (%s,%s,%s,2)
                    ON CONFLICT (cluster,grain) DO UPDATE SET next_ts=CASE
                    WHEN history_progress.quality_version<2 THEN excluded.next_ts
                    ELSE greatest(history_progress.next_ts,excluded.next_ts) END,
                    quality_version=2,updated_at=now()""",
                    (cluster, grain, end + grain),
                )

    async def progress(self, cluster, grain):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT next_ts,quality_version FROM history_progress WHERE cluster=%s AND grain=%s",
                    (cluster, grain),
                )
            ).fetchone()
            return row[0] if row and row[1] == QUALITY_VERSION else None

    async def refresh_rollups(self, now, budget=6):
        # Bootstrap statistics for samples already present when schema 4 was installed.
        for cluster in ("yz", "zw"):
            for grain in (60, 300, 3600):
                width = 86400 if grain == 3600 else 3600
                offset = 28800 if grain == 3600 else 0
                closed = (now + offset) // width * width - offset
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (7100 if cluster == "yz" else 7101,)
                    )
                    candidates = await (
                        await conn.execute(
                            """SELECT DISTINCT ((s.ts+%s)/%s)*%s-%s AS bucket
                        FROM history_entity e JOIN history_sample s ON s.entity_id=e.id
                        LEFT JOIN history_rollup r ON r.entity_id=e.id AND r.source_grain=s.grain
                            AND r.bucket=((s.ts+%s)/%s)*%s-%s
                        WHERE e.cluster=%s AND e.dimension='cluster' AND s.grain=%s
                            AND s.ts<%s AND r.entity_id IS NULL
                        ORDER BY bucket DESC LIMIT %s""",
                            (
                                offset,
                                width,
                                width,
                                offset,
                                offset,
                                width,
                                width,
                                offset,
                                cluster,
                                grain,
                                closed,
                                budget,
                            ),
                        )
                    ).fetchall()
                    for (bucket,) in candidates:
                        await aggregate(
                            conn,
                            cluster,
                            grain,
                            bucket,
                            bucket + width - 1,
                            now,
                            self.settings.stale_after_seconds,
                            self.settings.memory_capacity_unit != "unverified"
                            and self.settings.memory_capacity_unit == self.settings.memory_allocation_unit,
                        )

    async def prune(self, now):
        # Aggregate and delete whole buckets in one transaction, preserving sufficient statistics.
        for cluster in ("yz", "zw"):
            for grain, cutoff in ((60, now - 2 * 86400), (300, now - 8 * 86400)):
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (7100 if cluster == "yz" else 7101,)
                    )
                    rows = await (
                        await conn.execute(
                            """SELECT DISTINCT (ts/3600)*3600 FROM history_coverage
                        WHERE cluster=%s AND grain=%s AND ts<%s ORDER BY 1 LIMIT 24""",
                            (cluster, grain, cutoff // 3600 * 3600),
                        )
                    ).fetchall()
                    for (bucket,) in rows:
                        await aggregate(
                            conn,
                            cluster,
                            grain,
                            bucket,
                            bucket + 3599,
                            now,
                            self.settings.stale_after_seconds,
                            self.settings.memory_capacity_unit != "unverified"
                            and self.settings.memory_capacity_unit == self.settings.memory_allocation_unit,
                            True,
                        )
                        await conn.execute(
                            "DELETE FROM history_coverage WHERE cluster=%s AND grain=%s AND ts BETWEEN %s AND %s",
                            (cluster, grain, bucket, bucket + 3599),
                        )
        # Hourly samples and archived statistics retain all discovered history.
        async with self.pool.connection() as conn:
            await conn.execute("""DELETE FROM history_entity WHERE id IN (
                SELECT e.id FROM history_entity e WHERE NOT EXISTS
                (SELECT 1 FROM history_sample s WHERE s.entity_id=e.id) AND NOT EXISTS
                (SELECT 1 FROM history_rollup r WHERE r.entity_id=e.id)
                ORDER BY e.id FOR UPDATE SKIP LOCKED LIMIT 1000)""")

    async def status(self):
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute("""SELECT p.cluster,p.grain,p.next_ts,p.updated_at,p.quality_version,
                count(c.ts) FILTER (WHERE c.quality_version<2),
                count(c.ts) FILTER (WHERE c.status='healthy' AND c.last_attempt_status<>'healthy')
                FROM history_progress p LEFT JOIN history_coverage c ON c.cluster=p.cluster AND c.grain=p.grain
                GROUP BY p.cluster,p.grain ORDER BY p.cluster,p.grain""")
            ).fetchall()
        return [
            {
                "cluster": c,
                "grain": g,
                "next_ts": t,
                "updated_at": str(at),
                "quality_version": v,
                "remaining_legacy_points": legacy,
                "degraded_review_points": review,
            }
            for c, g, t, at, v, legacy, review in rows
        ]

    async def entities(self, cluster, grain, start, end, dimension):
        validate_scope(cluster, grain, dimension)
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    """SELECT e.entity_key FROM history_entity e
                WHERE e.cluster=%s AND e.dimension=%s AND EXISTS
                (SELECT 1 FROM history_sample s WHERE s.entity_id=e.id AND s.grain=%s AND s.ts BETWEEN %s AND %s)
                ORDER BY e.entity_key LIMIT 1001""",
                    (cluster, dimension, grain, start, end),
                )
            ).fetchall()

    async def reset_progress(self):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(7199)")
            await conn.execute("DELETE FROM history_progress")

    async def snapshot(self, cluster, conn=None):
        if conn is None:
            async with self.pool.connection() as connection:
                return await self.snapshot(cluster, connection)
        return await (
            await conn.execute(
                "SELECT payload,last_good,updated_at FROM resource_snapshot WHERE cluster=%s", (cluster,)
            )
        ).fetchone()

    async def save_snapshot(self, conn, cluster, payload, last_good, updated_at):
        if payload.get("status") == "healthy":
            last_good = None
        await conn.execute(
            """INSERT INTO resource_snapshot VALUES (%s,%s,%s,%s)
            ON CONFLICT (cluster) DO UPDATE SET payload=excluded.payload,
            last_good=excluded.last_good,updated_at=excluded.updated_at""",
            (cluster, Jsonb(payload), Jsonb(last_good) if last_good is not None else None, updated_at),
        )

    async def analysis_period(self, conn, cluster, window, dimension, scope, entity=None):
        """Closed SQL templates. Business attribution always sums workload facts once per time."""
        step = window.step
        coverage = await (
            await conn.execute(
                """SELECT count(*),count(*) FILTER (
            WHERE status='healthy' AND last_attempt_status='healthy' AND quality_version=2 AND source_age_seconds IS NOT NULL AND source_age_seconds<=%s)
            FROM history_coverage WHERE cluster=%s AND grain=%s AND ts BETWEEN %s AND %s
            AND (ts-%s) %% %s=0""",
                (
                    self.settings.stale_after_seconds,
                    cluster,
                    window.grain,
                    window.start,
                    window.end,
                    window.start,
                    step,
                ),
            )
        ).fetchone()
        business = dimension in ("namespace", "pod", "workload")
        detail = business and (
            dimension == "workload"
            or scope.node_name is not None
            or scope.device_uuid is not None
            or dimension == "namespace"
            and scope.pod_name is not None
        )
        source_dimension = "workload" if detail else dimension
        group_sql = {
            "namespace": "ARRAY[e.namespace_name]",
            "pod": "ARRAY[e.namespace_name,e.pod_name]",
        }.get(dimension, "e.entity_key")
        clauses = [
            "e.cluster=%s",
            "e.dimension=%s",
            "cardinality(e.entity_key)=%s",
            "array_position(e.entity_key,NULL) IS NULL",
            "NOT (''=ANY(e.entity_key))",
            "s.grain=%s",
            "s.ts BETWEEN %s AND %s",
            "(s.ts-%s) %% %s=0",
        ]
        params = [
            cluster,
            source_dimension,
            ARITY[source_dimension],
            window.grain,
            window.start,
            window.end,
            window.start,
            step,
        ]
        # Columns are exclusively the fixed fields in HistoryScope, never caller-provided SQL.
        for field, value in scope.model_dump(exclude_none=True).items():
            clauses.append("e." + field + "=%s")
            params.append(value)
        predicate = " AND ".join(clauses)
        entity_filter = "WHERE entity=%s" if entity is not None else ""
        if entity is not None:
            params.append(list(entity))
        capacity_sql = (
            "NULL::double precision"
            if business
            else "CASE WHEN count(*)=count(s.capacity) THEN sum(s.capacity) END"
        )
        query = f"""WITH points AS (
            SELECT {group_sql} AS entity,s.ts,{capacity_sql} AS capacity,
            CASE WHEN count(*)=count(s.allocated) AND bool_and(s.status='healthy' AND c.status='healthy' AND c.last_attempt_status='healthy' AND c.quality_version=2 AND c.source_age_seconds IS NOT NULL AND c.source_age_seconds<=%s)
                 THEN sum(s.allocated) END AS amount
            FROM history_entity e JOIN history_sample s ON s.entity_id=e.id
            JOIN history_coverage c ON (c.cluster,c.grain,c.ts)=(s.cluster,s.grain,s.ts)
            WHERE {predicate} GROUP BY entity,s.ts)
            SELECT entity,count(*),count(amount),sum(amount),sum(capacity),max(amount),min(capacity-amount),
            avg(CASE WHEN capacity>0 THEN amount/capacity END),count(*) FILTER (WHERE capacity>0 AND amount IS NOT NULL)
            FROM points {entity_filter} GROUP BY entity ORDER BY entity LIMIT 1001"""
        rows = await (await conn.execute(query, [self.settings.stale_after_seconds, *params])).fetchall()
        return coverage, rows

    async def validate_constraints(self):
        # Maintenance command only: fail without modifying invalid legacy rows.
        async with self.pool.connection() as conn:
            await conn.execute("SET LOCAL statement_timeout=120000")
            await conn.execute("ALTER TABLE history_entity VALIDATE CONSTRAINT history_entity_shape")
            await conn.execute("ALTER TABLE history_sample VALIDATE CONSTRAINT history_sample_semantics")
            await conn.execute("ALTER TABLE history_progress VALIDATE CONSTRAINT history_progress_scope")
            await conn.execute("ALTER TABLE resource_snapshot VALIDATE CONSTRAINT resource_snapshot_contract")
