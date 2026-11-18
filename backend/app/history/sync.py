import asyncio
import math
import time
from collections import defaultdict

from app.clients.prometheus import SourceError
from app.core.time_ranges import four_months_ago
from app.history.contracts import validate_scope
from app.services.normalizer import normalize


def grain_for(window):
    return {
        "1h": 60,
        "1d": 300,
        "24h": 300,
        "1w": 300,
        "7d": 300,
        "1mo": 3600,
        "3mo": 3600,
        "all": 3600,
        "4mo": 3600,
    }[window]


def bounds(now, grain):
    cutoff = {60: now - 2 * 86400, 300: now - 8 * 86400, 3600: four_months_ago(now)}[grain]
    return math.ceil(cutoff / grain) * grain, (now - 120) // grain * grain


def materialize(cluster, payloads, start, end, grain, settings):
    """Reuse instantaneous normalization at exact evaluation times, never forward-fill gaps."""
    vectors: dict[int, dict] = {
        t: {name: {"data": {"result": []}} for name in payloads} for t in range(start, end + 1, grain)
    }
    for name, payload in payloads.items():
        for row in payload["data"]["result"]:
            seen = set()
            for ts, value in row["values"]:
                if not math.isfinite(float(ts)) or ts != int(ts) or ts not in vectors or ts in seen:
                    raise SourceError("历史样本时间无效、重复或未对齐")
                seen.add(ts)
                vectors[ts][name]["data"]["result"].append({"metric": row["metric"], "value": [ts, value]})
    rows = []
    for ts, vector in vectors.items():
        inputs = (
            vector
            if any(vector[n]["data"]["result"] for n in ("capacity", "allocated", "workloads"))
            else {k: v for k, v in vector.items() if k != "freshness"}
        )
        summary = normalize(cluster, inputs, [], ts, settings)
        status = (
            "healthy" if summary.status == "healthy" else "empty" if summary.status == "empty" else "degraded"
        )
        notes = summary.warnings

        def append(dim, entity, cap, alloc, valid=True, ts=ts, status=status, notes=notes):
            rows.append((ts, dim, entity, cap, alloc, status if valid else "degraded", notes))

        capacity_keys = {
            (r["metric"].get("node"), r["metric"].get("device_uuid"))
            for r in vector["capacity"]["data"]["result"]
        }

        def spatial_values(gpus, capacity_keys=capacity_keys):
            # Match the historical PromQL: capacity sums capacity inventory only;
            # allocation requires complete matched sets and excludes orphan allocations.
            capacities = [g.capacity_raw for g in gpus if (g.node, g.device_uuid) in capacity_keys]
            amounts = [g.allocated_raw for g in gpus]
            cap = sum(capacities) if capacities and all(v is not None for v in capacities) else None
            matched = all((g.node, g.device_uuid) in capacity_keys for g in gpus)
            alloc = sum(amounts) if amounts and matched and all(v is not None for v in amounts) else None
            return cap, alloc

        append("cluster", (), *spatial_values(summary.gpus))
        nodes = defaultdict(list)
        for gpu in summary.gpus:
            append("gpu", (gpu.node, gpu.device_uuid), gpu.capacity_raw, gpu.allocated_raw, not gpu.warnings)
            nodes[gpu.node].append(gpu)
        for node, gpus in nodes.items():
            append("node", (node,), *spatial_values(gpus))
        groups: dict[tuple[str, tuple], list] = defaultdict(list)
        for w in summary.workloads:
            groups[("namespace", (w.namespace,))].append(w.allocated_raw)
            groups[("pod", (w.namespace, w.pod))].append(w.allocated_raw)
            groups[("workload", (w.node, w.device_uuid, w.namespace, w.pod, w.container))].append(
                w.allocated_raw
            )
        for (dim, entity), values in groups.items():
            valid = all(v is not None for v in values)
            append(dim, entity, None, sum(values) if valid else None, valid)
    return rows


class HistorySync:
    def __init__(self, resources, store):
        self.resources, self.store = resources, store
        self.settings = resources.settings
        self.last_status = "idle"
        self.next_tail = {}
        self.next_repair = {}
        self.next_prune = 0
        self.discovery_gate = asyncio.Lock()
        self.scope_status: dict[str, str] = {}

    async def discover_start(self, cluster, force=False):
        try:
            async with asyncio.timeout(30):
                return await self._discover_start(cluster, force)
        except TimeoutError:
            raise SourceError("历史起点探测超时") from None

    async def _discover_start(self, cluster, force=False):
        async with self.discovery_gate:
            existing = await self.store.earliest(cluster)
            if existing is not None and not force:
                return existing
            now = int(time.time())
            complete = True
            if self.settings.data_mode == "mock":
                earliest = four_months_ago(now)
            else:
                # Monthly existence windows include short-lived series between grid points.
                step = 30 * 86400
                right = now // 60 * 60
                left = max(60, right - 700 * step)
                left = right - ((right - left) // step) * step
                payload = await self.resources.client.range_query(cluster, "first_seen", left, right, step)
                complete = not (payload.get("warnings") or payload.get("infos"))
                found = [
                    int(t) for row in payload["data"]["result"] for t, v in row["values"] if float(v) > 0
                ]
                probe_template = "first_seen"
                if not complete:
                    probe_template = "source_probe"
                    p = await self.resources.client.range_query(cluster, "source_probe", left, right, step)
                    found.extend(
                        int(t) for row in p["data"]["result"] for t, v in row["values"] if float(v) > 0
                    )
                    if hasattr(self.store, "earliest_available"):
                        local = await self.store.earliest_available(cluster)
                        if local:
                            found.append(local)
                if not found:
                    raise SourceError("未发现可查询的历史起点")
                if not complete:
                    # Some Thanos stores drop older portions of a broad response. Probe smaller
                    # historical windows as well; quality remains limited until source recovery.
                    anchor = min(found)
                    for index in range(1, 13):
                        probe_left = max(60, anchor - index * step)
                        probe_right = anchor - (index - 1) * step
                        if probe_right < probe_left:
                            break
                        p = await self.resources.client.range_query(
                            cluster, "source_probe", probe_left, probe_right, 86400
                        )
                        found.extend(
                            int(t) for row in p["data"]["result"] for t, v in row["values"] if float(v) > 0
                        )
                high = min(found)
                low = max(60, high - step)
                while high - low > 60:
                    mid = ((low + high) // 120) * 60
                    p = await self.resources.client.range_query(cluster, probe_template, mid, mid, 60)
                    if p.get("warnings") or p.get("infos"):
                        complete = False
                    present = any(float(v) > 0 for row in p["data"]["result"] for _, v in row["values"])
                    if present:
                        high = mid
                    else:
                        low = mid
                if not complete:
                    # Existence of instantaneous values is not monotonic across gaps.
                    # Recheck the neighborhood so binary refinement cannot jump past
                    # an earlier visible segment when range stores returned partial data.
                    async def visible(ts):
                        try:
                            p = await self.resources.client.range_query(cluster, "source_probe", ts, ts, 3600)
                            return (
                                ts
                                if any(float(v) > 0 for row in p["data"]["result"] for _, v in row["values"])
                                else None
                            )
                        except SourceError:
                            return None

                    nearby = await asyncio.gather(
                        *(visible(high - i * 3600) for i in range(1, 73) if high - i * 3600 > 0)
                    )
                    high = min([high] + [t for t in nearby if t is not None])
                earliest = high
            await self.store.save_earliest(cluster, earliest, complete=bool(complete))
            return earliest

    async def ingest(self, cluster, grain, start, end, checkpoint=False):
        validate_scope(cluster, grain, "cluster", ())
        if (
            start <= 0
            or start % grain
            or end % grain
            or end < start
            or (end - start) // grain + 1 > self.settings.history_chunk_points
        ):
            raise SourceError("历史同步块超出限制")
        names = {
            "capacity": "device_capacity",
            "allocated": "device_allocated",
            "workloads": "workload_allocated",
            "freshness": "source_age",
        }
        results = await asyncio.gather(
            *(
                self.resources.client.range_query(cluster, template, start, end, grain)
                for template in names.values()
            )
        )
        # Warnings can mean partial Thanos data: do not replace a previously complete block.
        if any(p.get("warnings") or p.get("infos") for p in results):
            raise SourceError("历史同步源返回部分结果提示，进度未推进")
        payloads = dict(zip(names, results))
        try:
            rows = materialize(cluster, payloads, start, end, grain, self.settings)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            raise SourceError("历史维度响应格式或样本异常，进度未推进") from None
        ages = {}
        for row in payloads["freshness"]["data"]["result"]:
            for ts, value in row["values"]:
                age = float(value)
                if math.isfinite(age) and age >= 0:
                    ages[int(ts)] = age
        await self.store.write(cluster, grain, start, end, rows, checkpoint, source_ages=ages)

    async def refresh_recent(self, cluster, grain, first, end, budget=2):
        # Newest gaps first, separately from the durable oldest-first checkpoint.
        first = max(first, end - {60: 3600, 300: 7 * 86400, 3600: 124 * 86400}[grain])
        markers = await self.store.read(cluster, grain, first, end, grain)
        known = {r[0] for r in markers if r[3] in ("healthy", "empty")}
        right = end
        chunks = 0
        while right >= first and chunks < budget:
            if right in known:
                right -= grain
                continue
            left = max(first, right - (self.settings.history_chunk_points - 1) * grain)
            await self.ingest(cluster, grain, left, right)
            chunks += 1
            right = left - grain

    async def tick(self, budget=6, now=None):
        now = int(time.time()) if now is None else now
        # A session lock elects one sync writer even across multiple application workers.
        async with self.store.pool.connection() as conn:
            locked = await (await conn.execute("SELECT pg_try_advisory_lock(7199)")).fetchone()
            if not locked[0]:
                return
            try:
                scopes = [(cluster, grain) for cluster in ("yz", "zw") for grain in (60, 300, 3600)]
                # Fill recent windows in both clusters before slow old shards.
                for cluster, grain in scopes:
                    scope = f"{cluster}/{grain}"
                    self.scope_status[scope] = "ok"
                    first, end = bounds(now, grain)
                    try:
                        if grain == 3600:
                            first = math.ceil(await self.discover_start(cluster) / grain) * grain
                        if now >= self.next_tail.get(scope, 0):
                            self.next_tail[scope] = ((now - 120) // grain + 1) * grain + 120
                            await self.ingest(
                                cluster,
                                grain,
                                max(first, end - (min(2, self.settings.history_chunk_points) - 1) * grain),
                                end,
                            )
                        if now >= self.next_repair.get(scope, 0):
                            self.next_repair[scope] = now + max(
                                60, self.settings.history_sync_interval_seconds
                            )
                            await self.refresh_recent(cluster, grain, first, end)
                    except SourceError:
                        self.scope_status[scope] = "source_error"
                for cluster, grain in scopes:
                    scope = f"{cluster}/{grain}"
                    first, end = bounds(now, grain)
                    try:
                        if grain == 3600:
                            first = math.ceil(await self.discover_start(cluster) / grain) * grain
                        progress = await self.store.progress(cluster, grain)
                        start = max(first, progress if progress is not None else first)
                        for _ in range(budget):
                            if start > end:
                                break
                            right = min(end, start + (self.settings.history_chunk_points - 1) * grain)
                            await self.ingest(cluster, grain, start, right, True)
                            start = right + grain
                    except SourceError:
                        self.scope_status[scope] = "source_error"
                await self.store.refresh_rollups(now)
                if now >= self.next_prune:
                    await self.store.prune(now)
                    self.next_prune = now + 3600
                self.last_status = "ok" if all(v == "ok" for v in self.scope_status.values()) else "partial"
            finally:
                await conn.execute("SELECT pg_advisory_unlock(7199)")

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - isolated boundary; never log private exception details
                # Never expose credentials, SQL, private labels or upstream exception content.
                self.last_status = "error"
            await asyncio.sleep(self.settings.history_sync_interval_seconds)
