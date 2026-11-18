"""Agent task consistency probe against an isolated Mock PostgreSQL test database.

Uses a synthetic worker, never a model or Prometheus. Deletes only its own task IDs.
"""

import asyncio
import json
import os
from urllib.parse import urlsplit
from uuid import uuid4

from app.clients.prometheus import MockPrometheusClient, SourceError
from app.core.config import Settings
from app.history.service import HistoryService
from app.history.store import HistoryStore
from app.insights.models import DiagnosisRequest
from app.investigations.service import InvestigationService
from app.services.resources import ResourceService


async def discard(_):
    pass


async def main():
    url = os.environ.get("TEST_HISTORY_DATABASE_URL", "")
    if not url or not urlsplit(url).path.endswith("_test"):
        raise ValueError("requires an isolated database with a name ending in _test")
    settings = Settings(
        _env_file=None,
        data_mode="mock",
        history_database_url=url,
        memory_capacity_unit="MB",
        memory_allocation_unit="MB",
        allocation_absence_is_zero=True,
    )
    store = HistoryStore(settings)
    source = MockPrometheusClient(settings)
    ids = [uuid4(), uuid4()]
    pending = []
    initialized = False
    try:
        await store.open()
        await store.initialize()  # Rejects existing Live contracts before writes.
        initialized = True
        records = InvestigationService(
            HistoryService(ResourceService(source, settings), store), settings
        )
        query = DiagnosisRequest(cluster="yz", window="1h")
        entered, release = asyncio.Event(), asyncio.Event()
        worker_calls = 0

        async def work(emit):
            nonlocal worker_calls
            worker_calls += 1
            await emit(
                {"type": "evidence", "data": {"id": "synthetic-e1", "complete": True}}
            )
            entered.set()
            await release.wait()
            return {"status": "complete", "findings": [], "evidence": ["synthetic-e1"]}

        async def request():
            return await records.run("allocation_change", query, discard, work, ids[0])

        leader = asyncio.create_task(request())
        pending.append(leader)
        await asyncio.wait_for(entered.wait(), 5)
        contenders = await asyncio.gather(
            *(request() for _ in range(19)), return_exceptions=True
        )
        assert all(
            isinstance(r, SourceError) and "正在执行" in str(r) for r in contenders
        )
        release.set()
        first = await leader
        replays = await asyncio.gather(*(request() for _ in range(20)))
        assert worker_calls == 1 and all(r["investigation"]["reused"] for r in replays)
        assert all(r["evidence"] == first["evidence"] for r in replays)
        mismatch_rejected = False
        try:
            await records.run(
                "allocation_change",
                DiagnosisRequest(cluster="zw"),
                discard,
                work,
                ids[0],
            )
        except SourceError:
            mismatch_rejected = True
        assert mismatch_rejected

        cancel_entered = asyncio.Event()

        async def blocked(emit):
            await emit({"type": "evidence", "data": {"id": "synthetic-partial"}})
            cancel_entered.set()
            await asyncio.Event().wait()

        cancelled = asyncio.create_task(
            records.run("allocation_change", query, discard, blocked, ids[1])
        )
        pending.append(cancelled)
        await asyncio.wait_for(cancel_entered.wait(), 5)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        await records.finish(ids[1], "complete", {"status": "complete"})
        async with store.pool.connection() as conn:
            task_count = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM investigation_task WHERE id=%s", (ids[0],)
                    )
                ).fetchone()
            )[0]
            event_count = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM investigation_event WHERE task_id=%s",
                        (ids[0],),
                    )
                ).fetchone()
            )[0]
            state, result = await (
                await conn.execute(
                    "SELECT status,result FROM investigation_task WHERE id=%s",
                    (ids[1],),
                )
            ).fetchone()
            partial_count = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM investigation_event WHERE task_id=%s",
                        (ids[1],),
                    )
                ).fetchone()
            )[0]
        assert (task_count, event_count) == (1, 1)
        assert state == "cancelled" and result is None and partial_count == 1
        print(
            json.dumps(
                {
                    "kind": "synthetic_agent_task_consistency",
                    "database": "isolated_mock_test",
                    "simultaneous_same_id_requests": 20,
                    "worker_executions": worker_calls,
                    "running_duplicates_rejected": len(contenders),
                    "completed_replays": len(replays),
                    "additional_worker_executions_on_replay": worker_calls - 1,
                    "persisted_task_rows": task_count,
                    "persisted_evidence_events": event_count,
                    "different_request_same_id_rejected": mismatch_rejected,
                    "cancelled_state_preserved_after_late_finish": state == "cancelled",
                    "partial_evidence_retained": partial_count,
                    "external_model_calls": 0,
                    "limitation": "Synthetic worker tests task persistence only; not model accuracy or crash recovery.",
                },
                indent=2,
            )
        )
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if initialized and store.pool:
            async with store.pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM investigation_task WHERE id=ANY(%s)", (ids,)
                )
        await store.close()
        await source.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:  # noqa: BLE001 - never print credentials or raw database errors
        raise SystemExit(
            "任务一致性验证未完成，请检查独立Mock测试库；未调用外部服务。"
        ) from None
