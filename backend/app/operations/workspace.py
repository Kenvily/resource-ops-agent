"""Persist bounded, server-computed deliveries; never accept client-supplied metric facts."""

import asyncio
import hashlib
import json

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.clients.prometheus import SourceError
from app.operations.metrics import alerts


class WorkspaceError(SourceError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def size_guard(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 2_000_000:
        raise WorkspaceError("记录超过保存预算，请缩小范围", 422)
    return value


def record_row(row):
    return dict(zip(("id", "kind", "clusters", "request", "result", "created_at"), row, strict=True))


RECORD_COLUMNS = "id,kind,clusters,request,result,extract(epoch from created_at)"


class WorkspaceService:
    def __init__(self, history, settings, assessment, reports, metrics, investigations):
        self.pool = history.store.pool if history else None
        self.settings, self.assessment, self.reports = settings, assessment, reports
        self.metrics, self.investigations = metrics, investigations
        self.gate = asyncio.Semaphore(2)

    def scope(self, cluster):
        selected = ["yz", "zw"] if cluster == "all" else [cluster]
        if not set(selected).issubset(self.settings.insight_allowed_clusters):
            raise WorkspaceError("所选集群未获准查询", 403)
        if not self.pool:
            raise WorkspaceError("运营记录需要 PostgreSQL 数据库", 503)
        return selected

    def check_scope(self, clusters, selected):
        if not clusters or not set(clusters).issubset(selected):
            raise WorkspaceError("记录不存在或超出查询范围", 404)

    async def freeze_investigation(self, task_id, selected):
        try:
            detail = await self.investigations.detail(task_id)
        except SourceError:
            raise WorkspaceError("调查不存在或不在授权范围", 404) from None
        self.check_scope(detail["clusters"], selected)
        if detail["status"] not in ("complete", "limited", "refused"):
            raise WorkspaceError("仅可关联已有结论的调查", 422)
        # A frozen copy survives the original investigation's 90-day retention.
        keys = (
            "id",
            "kind",
            "status",
            "clusters",
            "request",
            "finished_at",
            "findings",
            "evidence",
            "warnings",
            "evidence_window",
            "feedback",
        )
        return size_guard({key: detail[key] for key in keys if key in detail})

    async def create_record(self, kind, query):
        selected = self.scope(query.requirement.cluster if kind == "assessment" else query.report.cluster)
        data = query.model_dump(mode="json", exclude={"request_id"})
        assert self.pool is not None
        async with self.pool.connection() as conn:
            # Serialize UUID retries without in-memory cache or duplicate computation.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(query.request_id),)
            )
            old = await (
                await conn.execute(
                    f"SELECT {RECORD_COLUMNS} FROM operations_record WHERE id=%s", (query.request_id,)
                )
            ).fetchone()
            if old:
                if old[1:4] != (kind, selected, data):
                    raise WorkspaceError("请求标识已用于其他内容")
                return await self.present_record(conn, old)
            if kind == "assessment":
                result = await self.assessment.evaluate(query.requirement)
            else:
                investigations = [
                    await self.freeze_investigation(t, selected) for t in query.investigation_ids
                ]
                issues = [await self.issue_detail(t, query.report.cluster) for t in query.issue_ids]
                result = await self.reports.generate(query.report)
                result["linked_investigations"] = investigations
                result["linked_issues"] = issues
                result["trace_warnings"] = []
                for item in investigations:
                    window = item.get("evidence_window") or {}
                    if (
                        not isinstance(window.get("start"), (int, float))
                        or not isinstance(window.get("end"), (int, float))
                        or window.get("start", 0) < result["target"]["start"]
                        or window.get("end", 0) > result["target"]["end"]
                    ):
                        result["trace_warnings"].append(
                            f"调查 {item['id']} 的证据范围未完全落在报告目标时段内，仅作背景"
                        )
                result["notes"].append("关联调查与问题保留各自时间、覆盖和受限状态，不替代报告时段统计。")
            result = jsonable_encoder(result)
            size_guard(result)
            row = await (
                await conn.execute(
                    f"INSERT INTO operations_record(id,kind,clusters,request,result) VALUES(%s,%s,%s,%s,%s) RETURNING {RECORD_COLUMNS}",
                    (query.request_id, kind, selected, Jsonb(data), Jsonb(result)),
                )
            ).fetchone()
            return await self.present_record(conn, row)

    async def present_record(self, conn, row):
        result = record_row(row)
        decisions = await (
            await conn.execute(
                "SELECT request_id,decision,candidates,note,extract(epoch from created_at) FROM operations_decision WHERE record_id=%s ORDER BY created_at,request_id LIMIT 200",
                (row[0],),
            )
        ).fetchall()
        result["decisions"] = [
            dict(zip(("request_id", "decision", "candidates", "note", "created_at"), d, strict=True))
            for d in decisions
        ]
        return result

    async def recent(self, kind, cluster):
        selected = self.scope(cluster)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id,kind,clusters,extract(epoch from created_at) FROM operations_record WHERE kind=%s AND clusters<@%s::text[] ORDER BY created_at DESC,id LIMIT 50",
                    (kind, selected),
                )
            ).fetchall()
        return {
            "records": [dict(zip(("id", "kind", "clusters", "created_at"), r, strict=True)) for r in rows]
        }

    async def detail(self, record_id, cluster="all"):
        selected = self.scope(cluster)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = await (
                await conn.execute(
                    f"SELECT {RECORD_COLUMNS} FROM operations_record WHERE id=%s", (record_id,)
                )
            ).fetchone()
            if not row:
                raise WorkspaceError("记录不存在", 404)
            self.check_scope(row[2], selected)
            return await self.present_record(conn, row)

    async def review(self, record_id, query):
        # An ID-based update requires the record's own scope, not access to both clusters.
        selected = list(self.settings.insight_allowed_clusters)
        if not self.pool:
            raise WorkspaceError("运营记录需要 PostgreSQL 数据库", 503)
        data = (record_id, query.decision, sorted(query.candidates), query.note)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            record = await (
                await conn.execute(
                    "SELECT clusters,kind,result FROM operations_record WHERE id=%s FOR UPDATE", (record_id,)
                )
            ).fetchone()
            if not record:
                raise WorkspaceError("记录不存在", 404)
            self.check_scope(record[0], selected)
            if record[1] == "report" and query.candidates:
                raise WorkspaceError("报告不接受候选选择", 422)
            if any(i >= len(record[2].get("candidates", [])) for i in query.candidates):
                raise WorkspaceError("候选索引不在保存的评估内", 422)
            if record[1] == "assessment" and query.decision == "accepted" and not query.candidates:
                raise WorkspaceError("请先选择至少一个候选", 422)
            old = await (
                await conn.execute(
                    "SELECT record_id,decision,candidates,note FROM operations_decision WHERE request_id=%s",
                    (query.request_id,),
                )
            ).fetchone()
            if old:
                if old != data:
                    raise WorkspaceError("复核请求标识内容冲突")
                return {"saved": True}
            count = await (
                await conn.execute(
                    "SELECT count(*) FROM operations_decision WHERE record_id=%s", (record_id,)
                )
            ).fetchone()
            if count[0] >= 200:
                raise WorkspaceError("本记录复核数量已达上限")
            await conn.execute(
                "INSERT INTO operations_decision(request_id,record_id,decision,candidates,note) VALUES(%s,%s,%s,%s,%s)",
                (query.request_id, *data),
            )
        return {"saved": True}

    async def capture_issue(self, query):
        selected = self.scope(query.cluster)
        metrics = await self.metrics.read(selected)
        candidates = alerts(metrics, self.settings.operations_gpu_temperature_c)
        match = next(
            (
                a
                for a in candidates
                if (a["metric"], a["node"], a["device_uuid"]) == (query.metric, query.node, query.device_uuid)
            ),
            None,
        )
        if not match:
            raise WorkspaceError("关注项已变化或过期，请重新读取指标", 409)
        fingerprint = hashlib.sha256(
            json.dumps([query.metric, query.node, query.device_uuid]).encode()
        ).hexdigest()
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (fingerprint + query.cluster,)
            )
            old = await (
                await conn.execute(
                    "SELECT id FROM operations_issue WHERE cluster=%s AND fingerprint=%s AND status IN ('open','investigating')",
                    (query.cluster, fingerprint),
                )
            ).fetchone()
            if old:
                return {"id": old[0], "reused": True}
            previous = await (
                await conn.execute(
                    "SELECT cluster,fingerprint FROM operations_issue WHERE id=%s", (query.request_id,)
                )
            ).fetchone()
            if previous:
                if previous != (query.cluster, fingerprint):
                    raise WorkspaceError("问题标识内容冲突")
                return {"id": query.request_id, "reused": True}
            await conn.execute(
                "INSERT INTO operations_issue(id,cluster,fingerprint,title,evidence) VALUES(%s,%s,%s,%s,%s)",
                (query.request_id, query.cluster, fingerprint, match["message"], Jsonb(match)),
            )
        return {"id": query.request_id, "reused": False}

    async def issues(self, cluster):
        selected = self.scope(cluster)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id,cluster,title,status,revision,extract(epoch from updated_at) FROM operations_issue WHERE cluster=ANY(%s) ORDER BY updated_at DESC,id LIMIT 100",
                    (selected,),
                )
            ).fetchall()
        return {
            "issues": [
                dict(zip(("id", "cluster", "title", "status", "revision", "updated_at"), r, strict=True))
                for r in rows
            ]
        }

    async def issue_detail(self, issue_id, cluster="all"):
        selected = self.scope(cluster)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = await (
                await conn.execute(
                    "SELECT id,cluster,title,status,revision,evidence,extract(epoch from created_at) FROM operations_issue WHERE id=%s",
                    (issue_id,),
                )
            ).fetchone()
            if not row:
                raise WorkspaceError("问题不存在", 404)
            self.check_scope([row[1]], selected)
            result = dict(
                zip(
                    ("id", "cluster", "title", "status", "revision", "evidence", "created_at"),
                    row,
                    strict=True,
                )
            )
            events = await (
                await conn.execute(
                    "SELECT request_id,revision,status,note,investigation,extract(epoch from created_at) FROM operations_issue_event WHERE issue_id=%s ORDER BY revision LIMIT 200",
                    (issue_id,),
                )
            ).fetchall()
            result["events"] = [
                dict(
                    zip(
                        ("request_id", "revision", "status", "note", "investigation", "created_at"),
                        r,
                        strict=True,
                    )
                )
                for r in events
            ]
        return result

    async def transition(self, issue_id, query):
        # An ID-based update requires the record's own scope, not access to both clusters.
        selected = list(self.settings.insight_allowed_clusters)
        if not self.pool:
            raise WorkspaceError("运营记录需要 PostgreSQL 数据库", 503)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT cluster,revision,status,fingerprint FROM operations_issue WHERE id=%s FOR UPDATE",
                    (issue_id,),
                )
            ).fetchone()
            if not row:
                raise WorkspaceError("问题不存在", 404)
            self.check_scope([row[0]], selected)
            old = await (
                await conn.execute(
                    "SELECT issue_id,revision,status,note,investigation FROM operations_issue_event WHERE request_id=%s",
                    (query.request_id,),
                )
            ).fetchone()
            prefix = (issue_id, query.revision + 1, query.status, query.note)
            if old:
                linked_id = old[4].get("id") if old[4] else None
                if old[:4] != prefix or linked_id != (
                    str(query.investigation_id) if query.investigation_id else None
                ):
                    raise WorkspaceError("更新标识内容冲突")
                return {"saved": True}
            if row[1] != query.revision:
                raise WorkspaceError("问题已被更新，请重新读取后提交")
            if row[1] >= 200:
                raise WorkspaceError("问题更新数量已达上限")
            if query.status in ("resolved", "dismissed") and not query.note:
                raise WorkspaceError("关闭问题时请填写核查结论", 422)
            if query.status in ("open", "investigating"):
                # Reopening cannot conflict with another active occurrence of the same signal.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (row[3] + row[0],))
                active = await (
                    await conn.execute(
                        "SELECT id FROM operations_issue WHERE cluster=%s AND fingerprint=%s AND status IN ('open','investigating') AND id<>%s",
                        (row[0], row[3], issue_id),
                    )
                ).fetchone()
                if active:
                    raise WorkspaceError("已有同类待核查问题，请使用现有问题")
            investigation = (
                await self.freeze_investigation(query.investigation_id, [row[0]])
                if query.investigation_id
                else None
            )
            used = await (
                await conn.execute(
                    "SELECT coalesce(sum(octet_length(investigation::text)),0) FROM operations_issue_event WHERE issue_id=%s",
                    (issue_id,),
                )
            ).fetchone()
            incoming = len(json.dumps(investigation, ensure_ascii=False).encode()) if investigation else 0
            if used[0] + incoming > 1_000_000:
                raise WorkspaceError("问题关联证据已达预算，请另建调查并在结论中说明", 422)
            await conn.execute(
                "INSERT INTO operations_issue_event(request_id,issue_id,revision,status,note,investigation) VALUES(%s,%s,%s,%s,%s,%s)",
                (query.request_id, *prefix, Jsonb(investigation) if investigation else None),
            )
            await conn.execute(
                "UPDATE operations_issue SET status=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                (query.status, issue_id),
            )
        return {"saved": True}
