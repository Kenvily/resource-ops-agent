import asyncio
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.clients.prometheus import SourceError

GOALS = {
    "resource_investigation": ["理解目标与时间范围", "查证资源、覆盖和替代解释", "核验结论并记录待核问题"],
    "allocation_change": [
        "检查时间范围与覆盖",
        "比较容量和分配基线",
        "下钻变化贡献与实体",
        "核查替代解释和结论限制",
    ],
    "gpu_usage": [
        "检查使用数据覆盖",
        "筛选候选及反例",
        "交叉核查整卡、容器和生命周期",
        "形成有证据的待核查结论",
    ],
}
STATES = {"complete", "limited", "refused", "cancelled", "failed", "interrupted", "waiting_input"}


class InvestigationService:
    def __init__(self, history, settings):
        self.pool = history.store.pool if history else None
        self.settings = settings

    def allowed(self, clusters):
        if not clusters or not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("调查不在当前获准范围")

    async def recover(self):
        if not self.pool:
            return
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE investigation_task SET status='interrupted',finished_at=now(),updated_at=now(),failure_reason='执行已中断；未自动重跑' WHERE status='running' AND deadline<now()"
            )
            await conn.execute(
                "DELETE FROM investigation_task WHERE status<>'running' AND created_at<now()-interval '90 days'"
            )

    async def begin(self, task_id, kind, clusters, query):
        self.allowed(clusters)
        assert self.pool is not None
        async with self.pool.connection() as conn:
            created = await (
                await conn.execute(
                    """INSERT INTO investigation_task(id,kind,status,clusters,request,deadline)
            VALUES(%s,%s,'running',%s,%s,now()+%s*interval '1 second') ON CONFLICT(id) DO NOTHING RETURNING id""",
                    (task_id, kind, clusters, Jsonb(query), self.settings.insight_timeout_seconds + 30),
                )
            ).fetchone()
            if created:
                return None
            old = await (
                await conn.execute(
                    "SELECT kind,clusters,request,status,result FROM investigation_task WHERE id=%s",
                    (task_id,),
                )
            ).fetchone()
            if not old or old[:3] != (kind, clusters, query):
                raise SourceError("任务标识与请求不一致")
            if old[3] == "running":
                raise SourceError("相同调查正在执行，请查看已有任务")
            if old[3] not in ("complete", "limited", "refused", "waiting_input") or old[4] is None:
                raise SourceError("原任务未完成，请使用新的任务标识重试")
            return old[4]

    async def event(self, task_id, sequence, event):
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO investigation_event(task_id,sequence,type,data) VALUES(%s,%s,%s,%s)",
                (task_id, sequence, event["type"], Jsonb(event["data"])),
            )

    async def finish(self, task_id, status, result=None, reason=None):
        if status not in STATES:
            raise SourceError("调查终态无效")
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE investigation_task SET status=%s,result=%s,failure_reason=%s,finished_at=now(),updated_at=now() WHERE id=%s AND status='running'",
                (status, Jsonb(result) if result is not None else None, reason, task_id),
            )

    async def run(self, kind, query, emit, work, task_id=None):
        if not self.pool:
            raise SourceError("调查记录需要 PostgreSQL")
        task_id = UUID(str(task_id)) if task_id else uuid4()
        clusters = ["yz", "zw"] if query.cluster == "all" else [query.cluster]
        request = query.model_dump(mode="json")
        cached = await self.begin(task_id, kind, clusters, request)
        if cached is not None:
            await emit({"type": "task", "data": {"id": str(task_id), "kind": kind, "goals": GOALS[kind]}})
            return {**cached, "investigation": {"id": str(task_id), "reused": True}}
        sequence = 0
        size = 0

        async def record(event):
            nonlocal sequence, size
            if event["type"] in ("progress", "evidence"):
                sequence += 1
                size += len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode())
                if sequence > 96 or size > 1_000_000:
                    raise SourceError("调查证据超过保存预算")
                await self.event(task_id, sequence, event)
            await emit(event)

        try:
            await emit({"type": "task", "data": {"id": str(task_id), "kind": kind, "goals": GOALS[kind]}})
            raw = await work(record)
            result = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 2_000_000:
                raise SourceError("调查结果超过保存预算")
            state = result.get("status", result.get("agent", {}).get("status", "limited"))
            state = state if state in ("complete", "refused", "waiting_input") else "limited"
            await self.finish(task_id, state, result)
            return {**result, "investigation": {"id": str(task_id), "status": state, "reused": False}}
        except asyncio.CancelledError:
            await asyncio.shield(
                self.finish(task_id, "cancelled", reason="用户取消或连接断开；已完成证据可查看")
            )
            raise
        except Exception:
            await self.finish(task_id, "failed", reason="调查未完成；请检查数据覆盖或服务状态")
            raise

    async def recent(self, cluster):
        await self.recover()
        clusters = ["yz", "zw"] if cluster == "all" else [cluster]
        self.allowed(clusters)
        if not self.pool:
            raise SourceError("需要 PostgreSQL")
        assert self.pool is not None
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id,kind,status,clusters,extract(epoch from created_at),legacy FROM investigation_task WHERE clusters<@%s::text[] ORDER BY created_at DESC,id DESC LIMIT 50",
                    (clusters,),
                )
            ).fetchall()
        return {
            "tasks": [
                {
                    "id": str(r[0]),
                    "kind": r[1],
                    "status": r[2],
                    "clusters": r[3],
                    "created_at": float(r[4]),
                    "legacy": r[5],
                }
                for r in rows
            ]
        }

    async def detail(self, task_id, cluster="all"):
        await self.recover()
        assert self.pool is not None
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = await (
                await conn.execute(
                    "SELECT kind,status,clusters,request,result,extract(epoch from created_at),extract(epoch from finished_at),failure_reason,legacy FROM investigation_task WHERE id=%s",
                    (task_id,),
                )
            ).fetchone()
            if not row:
                raise SourceError("调查不存在或已超过保留期")
            self.allowed(row[2])
            if cluster != "all" and not set(row[2]).issubset({cluster}):
                raise SourceError("调查不在当前筛选范围")
            events = await (
                await conn.execute(
                    "SELECT sequence,type,data FROM investigation_event WHERE task_id=%s ORDER BY sequence",
                    (task_id,),
                )
            ).fetchall()
            feedback = await (
                await conn.execute(
                    "SELECT decision,note,extract(epoch from created_at) FROM investigation_feedback WHERE task_id=%s ORDER BY created_at DESC LIMIT 20",
                    (task_id,),
                )
            ).fetchall()
        result = row[4] or {}
        proof = result.get("evidence", result.get("agent", {}).get("evidence", []))
        if not proof:
            proof = [e[2] for e in events if e[1] == "evidence"]
        findings = result.get("findings", result.get("agent", {}).get("findings", []))
        normalized = []
        for item in findings:
            refs = [r for r in item.get("evidence_ids", []) if r in {e["id"] for e in proof}]
            unknown = (
                item.get("kind") == "insufficient" or not refs or "insufficient" in item.get("outcome", "")
            )
            normalized.append(
                {
                    "title": item.get("title", "待核查结论"),
                    "level": item.get("level")
                    if item.get("level") in ("fact", "supported_explanation", "hypothesis", "unknown")
                    else "unknown"
                    if unknown
                    else "fact"
                    if row[0] == "allocation_change"
                    else "inference",
                    "evidence_ids": refs,
                    "limitations": item.get("limitations", []),
                }
            )
        windows = result.get("windows", {}).get("full", {})
        evidence_window = (
            {"start": windows["baseline"]["start"], "end": windows["target"]["end"]}
            if windows.get("baseline") and windows.get("target")
            else {"start": result.get("start"), "end": result.get("end")}
        )
        return {
            "warnings": result.get("warnings", result.get("agent", {}).get("warnings", [])),
            "evidence_window": evidence_window,
            "id": str(task_id),
            "kind": row[0],
            "status": row[1],
            "clusters": row[2],
            "request": row[3],
            "result": result,
            "created_at": float(row[5]),
            "finished_at": float(row[6]) if row[6] else None,
            "failure_reason": row[7],
            "legacy": row[8],
            "goals": GOALS[row[0]],
            "events": [{"sequence": e[0], "type": e[1], "data": e[2]} for e in events],
            "evidence": proof,
            "findings": normalized,
            "feedback": [{"decision": f[0], "note": f[1], "created_at": float(f[2])} for f in feedback],
            "feedback_enabled": self.settings.investigation_feedback_enabled,
            "metadata_version": 1,
            "execution": result.get("execution", result.get("agent", {}).get("execution")),
        }

    async def feedback(self, task_id, query):
        if not self.settings.investigation_feedback_enabled:
            raise SourceError("人工反馈尚未启用")
        report = await self.detail(task_id)
        if report["status"] == "running":
            raise SourceError("调查执行中，暂不能反馈")
        assert self.pool is not None
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO investigation_feedback(request_id,task_id,decision,note) VALUES(%s,%s,%s,%s) ON CONFLICT(request_id) DO NOTHING RETURNING request_id",
                    (query.request_id, task_id, query.decision, query.note),
                )
            ).fetchone()
            if not row:
                old = await (
                    await conn.execute(
                        "SELECT task_id,decision,note FROM investigation_feedback WHERE request_id=%s",
                        (query.request_id,),
                    )
                ).fetchone()
                if old != (task_id, query.decision, query.note):
                    raise SourceError("反馈请求标识已用于不同内容")
        return {"saved": True}
