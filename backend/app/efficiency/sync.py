"""One optional bounded collector; evaluation never triggers source requests."""

import asyncio
import time

from app.clients.prometheus import SourceError
from app.efficiency.container import normalize_containers
from app.efficiency.normalize import normalize
from app.efficiency.queries import METRICS, OPTIONAL_METRICS


class EfficiencySync:
    def __init__(self, resources, store):
        self.resources, self.store = resources, store
        self.status = "idle"
        self.scopes = {}

    async def ingest(self, cluster, start, end):
        if start % 60 or end % 60 or end < start or (end - start) // 60 + 1 > 30:
            raise SourceError("使用指标同步块超出限制")
        try:
            names = list(METRICS) + list(OPTIONAL_METRICS)
            payloads = await asyncio.gather(
                *(self.resources.client.range_query(cluster, name, start, end, 60) for name in names),
                return_exceptions=True,
            )
            required = dict(zip(names[: len(METRICS)], payloads[: len(METRICS)]))
            if any(isinstance(payload, BaseException) for payload in required.values()):
                raise SourceError("使用指标查询未完成")
            rows = normalize(required, cluster, start, end, self.resources.settings)
            optional = {
                name: value
                for name, value in zip(names, payloads)
                if name in OPTIONAL_METRICS and not isinstance(value, BaseException)
            }
            containers = normalize_containers(
                optional, required, rows, cluster, start, end, self.resources.settings
            )
        except (SourceError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
            await self.store.write(cluster, start, end, [], "使用指标采集失败、部分响应或关联异常")
            return False
        await self.store.write(cluster, start, end, rows, containers=containers)
        return bool(rows) and all(r["verified"] for r in rows)

    async def tick(self, now=None):
        now = int(time.time()) if now is None else now
        end = (now - 120) // 60 * 60
        for cluster, lock in (("yz", 7290), ("zw", 7291)):
            async with self.store.pool.connection() as conn:
                locked = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock,))).fetchone()
                if not locked[0]:
                    continue
                try:
                    # Cross-process cadence: a recent persisted attempt suppresses another collector.
                    recent = await (
                        await conn.execute(
                            "SELECT max(collected_at)>now()-interval '55 seconds' FROM efficiency_coverage WHERE cluster=%s AND ts=%s",
                            (cluster, end),
                        )
                    ).fetchone()
                    if recent[0]:
                        continue
                    okay = await self.ingest(cluster, end - 60, end)
                    missing = await self.store.missing(cluster, end - 24 * 3600, end - 120)
                    # Latest gaps first, 2*30 minutes per tick; bounded even when upstream is empty.
                    for _ in range(2):
                        if not missing:
                            break
                        right = max(missing)
                        left = max(end - 24 * 3600, right - 29 * 60)
                        okay = await self.ingest(cluster, left, right) and okay
                        missing.difference_update(range(left, right + 1, 60))
                    self.scopes[cluster] = "ok" if okay else "partial"
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))
        await self.store.prune(now)
        self.status = "ok" if self.scopes and all(s == "ok" for s in self.scopes.values()) else "partial"

    async def run(self):
        while True:
            try:
                async with asyncio.timeout(90):
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep private labels and database credentials out of logs
                self.status = "error"
            await asyncio.sleep(60)
