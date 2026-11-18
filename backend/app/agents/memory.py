"""Structured task memory, private entity bindings and conservative per-chain reservations."""

import hashlib
import json

from psycopg.types.json import Jsonb

from app.clients.prometheus import SourceError


class TaskMemory:
    def __init__(self, history, records):
        self.history, self.records = history, records
        self.pool = history.store.pool

    def fingerprint(self):
        scope = {
            "contract": self.history.store.contract(),
            "clusters": self.records.settings.insight_allowed_clusters,
            "execution_limits": [
                self.records.settings.insight_max_tool_calls,
                self.records.settings.insight_max_model_rounds,
                self.records.settings.insight_timeout_seconds,
            ],
        }
        return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()

    async def load(self, parent_id):
        detail = await self.records.detail(parent_id)
        if detail["status"] == "running":
            raise SourceError("原任务仍在运行")
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT root_id,scope_fingerprint,memory,bindings FROM agent_task_memory WHERE task_id=%s",
                    (parent_id,),
                )
            ).fetchone()
        if row and row[1] != self.fingerprint():
            raise SourceError("任务数据源或授权范围已变化，请新建调查")
        legacy_bindings = {}
        for entity in detail.get("result", {}).get("entities", []):
            if (
                entity.get("cluster") in detail["clusters"]
                and entity.get("dimension") in ("node", "gpu")
                and len(entity.get("key", [])) == (2 if entity["dimension"] == "gpu" else 1)
            ):
                legacy_bindings[entity["id"]] = {
                    "cluster": entity["cluster"],
                    "dimension": entity["dimension"],
                    "key": entity["key"],
                }
        for i, item in enumerate(detail.get("result", {}).get("results", [])):
            if item.get("cluster") in detail["clusters"] and item.get("node") and item.get("device_uuid"):
                ref = item.get("investigation", {}).get("entity_id", f"legacy-gpu-{i + 1}")
                legacy_bindings[ref] = {
                    "cluster": item["cluster"],
                    "dimension": "gpu",
                    "key": [item["node"], item["device_uuid"]],
                }
        # Legacy contexts may identify an object; old measurements never become fresh evidence.
        return {
            "root_id": row[0] if row else parent_id,
            "clusters": detail["clusters"],
            "request": detail["request"],
            "memory": row[2]
            if row
            else {"metrics": ["gpu_util"] if detail["kind"] == "gpu_usage" else ["allocation"]},
            "bindings": row[3] if row else legacy_bindings,
            "legacy": not bool(row),
        }

    async def reserve(self, task_id, parent_id, parent):
        root = parent["root_id"] if parent and not parent["legacy"] else task_id
        async with self.pool.connection() as conn:
            if root != task_id:
                row = await (
                    await conn.execute(
                        "UPDATE agent_task_memory SET turns_reserved=turns_reserved+1 WHERE task_id=%s AND turns_reserved<4 RETURNING turns_reserved",
                        (root,),
                    )
                ).fetchone()
                if not row:
                    raise SourceError("任务链已达到4轮预算，请明确新目标后新建调查")
            await conn.execute(
                "INSERT INTO agent_task_memory(task_id,root_id,parent_id,scope_fingerprint) VALUES(%s,%s,%s,%s)",
                (task_id, root, parent_id, self.fingerprint()),
            )
        return root

    async def save(self, task_id, summary, bindings):
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_task_memory SET memory=%s,bindings=%s,updated_at=now() WHERE task_id=%s",
                (Jsonb(summary), Jsonb(bindings), task_id),
            )
