"""Time statistics over trusted samples. Sparse coverage remains explicit, never forward-filled."""


async def aggregate(conn, cluster, grain, start, end, now, stale_after, units_verified, finalize=False):
    width = 86400 if grain == 3600 else 3600
    offset = 28800 if width == 86400 else 0
    first = (start + offset) // width * width - offset
    last = min((end + offset) // width * width - offset, ((now + offset) // width - 1) * width - offset)
    if last < first:
        return
    await conn.execute(
        """
    INSERT INTO history_rollup(entity_id,source_grain,bucket,bucket_seconds,expected_count,
        covered_count,valid_count,metrics,finalized)
    WITH facts AS (
        SELECT s.*, ((s.ts+%s)/%s)*%s-%s AS bucket,
            c.status='healthy' AND c.last_attempt_status='healthy' AND c.quality_version=2
            AND c.source_age_seconds IS NOT NULL AND c.source_age_seconds<=%s
            AND s.status='healthy' AS trusted
        FROM history_sample s JOIN history_coverage c USING(cluster,grain,ts)
        WHERE s.cluster=%s AND s.grain=%s AND s.ts BETWEEN %s AND %s
    ), coverage AS (
        SELECT ((ts+%s)/%s)*%s-%s AS bucket,count(*) FILTER(WHERE status='healthy'
            AND last_attempt_status='healthy' AND quality_version=2
            AND source_age_seconds IS NOT NULL AND source_age_seconds<=%s) AS covered
        FROM history_coverage WHERE cluster=%s AND grain=%s AND ts BETWEEN %s AND %s GROUP BY 1
    ), stats AS (
        SELECT f.entity_id,f.bucket,m.name,count(*) FILTER(WHERE f.trusted AND m.value IS NOT NULL) AS n,
            sum(m.value) FILTER(WHERE f.trusted) AS total,
            min(m.value) FILTER(WHERE f.trusted) AS minimum,
            max(m.value) FILTER(WHERE f.trusted) AS maximum,
            count(*) FILTER(WHERE f.trusted AND f.allocated IS NOT NULL AND
                (f.dimension IN ('namespace','pod','workload') OR f.capacity IS NOT NULL)) AS valid
        FROM facts f CROSS JOIN LATERAL (VALUES
            ('capacity',f.capacity),('allocated',f.allocated),
            ('unallocated',CASE WHEN %s THEN f.capacity-f.allocated END),
            ('ratio',CASE WHEN %s AND f.capacity>0 THEN f.allocated/f.capacity END)
        ) m(name,value) GROUP BY f.entity_id,f.bucket,m.name
    ) SELECT stats.entity_id,%s,stats.bucket,%s,%s,max(coverage.covered),max(stats.valid),
        jsonb_object_agg(stats.name,jsonb_build_object('sum',stats.total,'mean',stats.total/nullif(stats.n,0),
            'min',stats.minimum,'max',stats.maximum,'valid_count',stats.n,
            'coverage',stats.n::float/%s)),%s
        FROM stats JOIN coverage USING(bucket) GROUP BY stats.entity_id,stats.bucket
    ON CONFLICT(entity_id,source_grain,bucket) DO UPDATE SET
        covered_count=excluded.covered_count,valid_count=excluded.valid_count,
        metrics=excluded.metrics,finalized=excluded.finalized,updated_at=now()
    WHERE NOT history_rollup.finalized
    """,
        (
            offset,
            width,
            width,
            offset,
            stale_after,
            cluster,
            grain,
            first,
            last + width - 1,
            offset,
            width,
            width,
            offset,
            stale_after,
            cluster,
            grain,
            first,
            last + width - 1,
            units_verified,
            units_verified,
            grain,
            width,
            width // grain,
            width // grain,
            finalize,
        ),
    )
