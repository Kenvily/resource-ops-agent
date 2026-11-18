"""Bounded, database-only evidence services for future controlled Agent tools."""

import time

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.clients.prometheus import SourceError
from app.history.contracts import HistoryScope, validate_scope
from app.history.sync import bounds
from app.models.history_analysis import AnalysisWindow, ChangeEvidence, HistoryComparison, PeriodMetrics


class HistoryAnalysisService:
    def __init__(self, resources, store):
        self.resources, self.store = resources, store

    def validate(self, cluster, dimension, baseline, target, scope, entity):
        validate_scope(cluster, baseline.grain, dimension, entity)
        if baseline.grain != target.grain or baseline.step != target.step:
            raise SourceError("历史比较必须使用一致粒度和步长")
        now = int(time.time())
        for window in (baseline, target):
            if (
                window.start < (1 if window.grain == 3600 else bounds(now, window.grain)[0])
                or window.end > now
            ):
                raise SourceError("分析时间超出该粒度保留范围或位于未来")
        if dimension in ("cluster", "node", "gpu") and (scope.namespace_name or scope.pod_name):
            raise SourceError("业务筛选不能用于推断整卡容量")
        if dimension == "node" and scope.device_uuid:
            raise SourceError("节点汇总不支持按单卡筛选")
        if dimension == "cluster" and any(scope.model_dump(exclude_none=True).values()):
            raise SourceError("集群汇总不能作为子范围汇总")

    def metrics(self, window, coverage, row, dimension):
        expected = window.expected_points
        all_covered = coverage[0] == expected and coverage[1] == expected
        business = dimension in ("namespace", "pod", "workload")
        absent_zero = business and self.resources.settings.allocation_absence_is_zero and all_covered
        present, valid = (row[1], row[2]) if row else (0, 0)
        absent = expected - present
        complete = all_covered and valid == present and (present == expected or absent_zero)
        # No inference of zero from a missing coverage marker or a degraded acquisition.
        values = {}
        if complete:
            values["allocated_mean_raw"] = (row[3] if row else 0) / expected
            values["allocated_max_raw"] = max(row[5] if row else 0, 0) if absent else row[5]
            if not business:
                values["capacity_mean_raw"] = row[4] / expected if row[4] is not None else None
                if self.resources.units().unit_verified:
                    values["unallocated_min_raw"] = row[6]
                    values["ratio_mean"] = row[7] if row[8] == expected else None
        return PeriodMetrics(
            expected_points=expected,
            observed_points=min(coverage[1], valid),
            absent_points=absent,
            covered_points=coverage[0],
            verified_coverage_points=coverage[1],
            zero_inferred=complete and absent > 0 and absent_zero,
            complete=complete,
            **values,
        )

    async def gpu_history(self, cluster, window: AnalysisWindow, node=None):
        """Bounded per-device evidence; no current Top-K truncation or upstream queries."""
        return await self._compare(cluster, window, window, "gpu", None, HistoryScope(node_name=node), None)

    async def compare_windows(
        self,
        cluster,
        baseline: AnalysisWindow,
        target: AnalysisWindow,
        dimension="cluster",
        entity=(),
        scope: HistoryScope | None = None,
    ):
        return await self._compare(
            cluster, baseline, target, dimension, entity, scope or HistoryScope(), None
        )

    async def allocation_changes(
        self,
        cluster,
        baseline: AnalysisWindow,
        target: AnalysisWindow,
        dimension="namespace",
        scope: HistoryScope | None = None,
        top_k=10,
    ):
        if (
            dimension not in ("node", "gpu", "namespace", "pod")
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 100
        ):
            raise SourceError("变化贡献维度或数量无效")
        return await self._compare(cluster, baseline, target, dimension, None, scope or HistoryScope(), top_k)

    async def _compare(self, cluster, baseline, target, dimension, entity, scope, top_k):
        self.validate(cluster, dimension, baseline, target, scope, entity)
        try:
            async with self.store.pool.connection() as conn:
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                left, lrows = await self.store.analysis_period(
                    conn, cluster, baseline, dimension, scope, entity
                )
                right, rrows = await self.store.analysis_period(
                    conn, cluster, target, dimension, scope, entity
                )
        except (DatabaseError, PoolTimeout, OSError):
            raise SourceError("历史分析数据库不可用或查询预算超限") from None
        truncated = len(lrows) > 1000 or len(rrows) > 1000
        warnings = []
        if not self.resources.units().unit_verified:
            warnings.append("单位未确认，仅提供原始值统计，不推断剩余额度或分配率")
        if truncated:
            warnings.append("历史候选实体超过预算，请收窄节点或业务范围；未生成全局排名")
        unknown = not lrows and not rrows
        if unknown:
            warnings.append("实体或筛选范围在两个窗口均无记录，不能推断为已知资源")
        changes = []
        keys = {tuple(r[0]) for r in lrows + rrows} if entity is None else {tuple(entity)}
        truncated = truncated or len(keys) > 1000
        if truncated and not any("候选" in note for note in warnings):
            warnings.append("候选联合范围超过预算，请收窄范围；未生成全局排名")
        lm, rm = {tuple(r[0]): r for r in lrows}, {tuple(r[0]): r for r in rrows}
        for key in sorted(keys) if not truncated else []:
            a = self.metrics(baseline, left, lm.get(key), dimension)
            b = self.metrics(target, right, rm.get(key), dimension)
            if unknown:
                a = PeriodMetrics(
                    expected_points=baseline.expected_points,
                    observed_points=0,
                    absent_points=baseline.expected_points,
                    covered_points=left[0],
                    verified_coverage_points=left[1],
                    complete=False,
                )
                b = PeriodMetrics(
                    expected_points=target.expected_points,
                    observed_points=0,
                    absent_points=target.expected_points,
                    covered_points=right[0],
                    verified_coverage_points=right[1],
                    complete=False,
                )
            changes.append(
                ChangeEvidence(
                    entity=key,
                    baseline=a,
                    target=b,
                    allocated_change_raw=b.allocated_mean_raw - a.allocated_mean_raw
                    if a.complete and b.complete
                    else None,
                    capacity_change_raw=b.capacity_mean_raw - a.capacity_mean_raw
                    if a.capacity_mean_raw is not None and b.capacity_mean_raw is not None
                    else None,
                )
            )
        complete = (
            not truncated
            and not unknown
            and left[1] == baseline.expected_points
            and right[1] == target.expected_points
            and all(c.baseline.complete and c.target.complete for c in changes)
        )
        if not complete:
            warnings.append("存在未同步、旧口径或质量缺测，未据此推断变化贡献")
        cohort_change = (
            sum(c.allocated_change_raw for c in changes if c.allocated_change_raw is not None)
            if complete
            else None
        )
        entity_count = None if truncated else len(keys)
        if top_k:
            changes = sorted(
                changes,
                key=lambda c: (c.allocated_change_raw is not None, abs(c.allocated_change_raw or 0)),
                reverse=True,
            )[:top_k]
        if dimension in ("namespace", "pod", "workload"):
            warnings.append("按名称标签关联；未接入 Pod UID/生命周期，不将同名实体变化直接解释为同一任务因果")
        return HistoryComparison(
            cluster=cluster,
            dimension=dimension,
            scope=scope,
            baseline_window=baseline,
            target_window=target,
            units=self.resources.units(),
            complete=complete,
            truncated=truncated,
            entity_count=entity_count,
            omitted_entities=entity_count - len(changes) if entity_count is not None else None,
            cohort_allocated_change_raw=cohort_change,
            changes=changes,
            warnings=warnings,
        )
