"""Immutable, scoped investigation reports; no human review operations."""

import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.clients.prometheus import SourceError


class InvestigationStore:
    def __init__(self, store, settings):
        self.pool, self.settings = store.pool, settings

    def allowed(self, clusters):
        if not set(clusters).issubset(self.settings.insight_allowed_clusters):
            raise SourceError("调查记录超出当前获准集群范围")

    async def save_run(self, report):
        run_id = uuid4()
        report["run_id"] = str(run_id)
        for item in report["results"]:
            item["case_id"] = str(uuid4())
        if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode()) > 1_000_000:
            raise SourceError("调查报告超出保存预算，请缩短观察范围")
        clusters = [c["cluster"] for c in report["clusters"]]
        self.allowed(clusters)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO efficiency_investigation(id,clusters,request,result) VALUES(%s,%s,%s,%s)",
                (run_id, clusters, Jsonb(report["request"]), Jsonb(report)),
            )
            for item in report["results"]:
                await conn.execute(
                    "INSERT INTO efficiency_case VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        UUID(item["case_id"]),
                        item["cluster"],
                        item["node"],
                        item["device_uuid"],
                        sorted(
                            {
                                w["pod_uid"]
                                for w in item["workloads"] + item.get("task_candidates", [])
                                if w["pod_uid"]
                            }
                        ),
                        "review_candidate" if item.get("task_candidates") else item["classification"],
                    ),
                )
        return report

    async def load(self, run_id):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT clusters,result FROM efficiency_investigation WHERE id=%s", (run_id,)
                )
            ).fetchone()
        if not row:
            raise SourceError("调查记录不存在或已超过保留期")
        self.allowed(row[0])
        return row[1]

    async def recent(self, cluster):
        selected = ["yz", "zw"] if cluster == "all" else [cluster]
        self.allowed(selected)
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT id,extract(epoch from created_at),clusters,request->>'hours',
                result->'agent'->>'mode' FROM efficiency_investigation WHERE clusters<@%s::text[]
                ORDER BY created_at DESC LIMIT 20""",
                    (selected,),
                )
            ).fetchall()
        return {
            "reports": [
                {
                    "id": str(r[0]),
                    "created_at": float(r[1]),
                    "clusters": r[2],
                    "hours": int(r[3]),
                    "mode": r[4],
                }
                for r in rows
            ]
        }
