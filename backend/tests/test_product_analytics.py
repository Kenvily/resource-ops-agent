# ruff: noqa: F811
import hashlib
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from test_analysis import context  # noqa: F401

from app.analytics.models import EventBatch
from app.analytics.service import AnalyticsService
from app.core.config import Settings
from app.main import create_app


@pytest_asyncio.fixture
async def analytics(context):
    store, source, history, *_ = context
    async with store.pool.connection() as conn:
        await conn.execute("TRUNCATE product_event,investigation_task CASCADE")
        await conn.execute("UPDATE product_analytics_metadata SET first_event_at=NULL")
    yield AnalyticsService(history, source.settings), store, source, history


def batch(visitor=None, events=None):
    return EventBatch(
        visitor_id=visitor or uuid4(),
        events=events or [{"id": uuid4(), "page": "workbench", "kind": "page_view"}],
    )


@pytest.mark.parametrize(
    "event",
    [
        {"id": str(uuid4()), "page": "https://private/path", "kind": "page_view"},
        {"id": str(uuid4()), "page": "workbench", "kind": "page_view", "question": "private"},
        {"id": str(uuid4()), "page": "workbench", "kind": "page_view", "feature": "assistant_query"},
        {"id": str(uuid4()), "page": "workbench", "kind": "feature_use", "feature": "assistant_query"},
        {"id": str(uuid4()), "page": "workbench", "kind": "feature_use"},
    ],
)
def test_only_registered_non_private_event_fields_allowed(event):
    with pytest.raises(ValidationError):
        batch(events=[event])


async def test_idempotent_events_hash_visitors_and_do_not_inflate_counts(analytics):
    service, store, *_ = analytics
    visitor = uuid4()
    first = batch(
        visitor,
        [
            {"id": uuid4(), "kind": "page_view", "page": "analysis.assistant"},
            {
                "id": uuid4(),
                "kind": "feature_use",
                "page": "analysis.assistant",
                "feature": "assistant_query",
            },
        ],
    )
    assert (await service.record(first))["accepted"] == 2
    assert (await service.record(first))["accepted"] == 0
    await service.record(batch(visitor))
    await service.record(batch())
    report = await service.summary(7)
    assert (report["pv"], report["uv"], report["dau"], report["feature_uses"]) == (3, 2, 2, 1)
    async with store.pool.connection() as conn:
        hashes = await (await conn.execute("SELECT DISTINCT visitor_hash FROM product_event")).fetchall()
    assert (hashlib.sha256(("roc-product-v1:" + str(visitor)).encode()).hexdigest(),) in hashes
    assert str(visitor) not in str(hashes)
    assert report["agents"]["success_rate"] is None and report["agents"]["avg_duration_ms"] is None
    assert report["daily"][0]["pv"] is None  # Before tracking started is unknown, not zero.


async def test_shanghai_midnight_uv_is_not_sum_of_dau_and_task_outcomes_are_honest(analytics):
    service, store, *_ = analytics
    now = datetime(2026, 11, 6, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = now.replace(day=5, hour=23, minute=59)
    midnight = now.replace(hour=0, minute=1)
    async with store.pool.connection() as conn:
        for visitor, at in [("a" * 64, first), ("a" * 64, midnight), ("b" * 64, midnight)]:
            await conn.execute(
                "INSERT INTO product_event(id,visitor_hash,occurred_at,page,kind) VALUES(%s,%s,%s,'workbench','page_view')",
                (uuid4(), visitor, at),
            )
        await conn.execute("UPDATE product_analytics_metadata SET first_event_at=%s", (first,))
        for status, seconds, calls, legacy in [
            ("complete", 10, 2, False),
            ("limited", 20, 0, False),
            ("cancelled", 30, None, False),
            ("running", None, None, False),
            ("complete", 0, 1, True),
        ]:
            created = now - timedelta(minutes=10)
            finished = created + timedelta(seconds=seconds) if seconds is not None else None
            from psycopg.types.json import Jsonb

            await conn.execute(
                "INSERT INTO investigation_task(id,kind,status,clusters,request,result,created_at,deadline,finished_at,legacy) VALUES(%s,'resource_investigation',%s,ARRAY['yz'],'{}',%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    status,
                    Jsonb({"model_calls": calls}) if calls is not None else None,
                    created,
                    now + timedelta(hours=1),
                    finished,
                    legacy,
                ),
            )
    report = await service.summary(7, now)
    assert report["pv"] == 3 and report["uv"] == 2 and report["dau"] == 2
    assert report["daily"][-2]["dau"] == 1 and report["daily"][-1]["dau"] == 2
    agent = report["agents"]
    assert agent["tasks"] == 4 and agent["terminal_tasks"] == 3
    assert agent["success_rate"] == pytest.approx(1 / 3)
    assert agent["avg_duration_ms"] == 20000 and agent["duration_samples"] == 3
    assert agent["model_tasks"] == 1 and agent["unknown_model_tasks"] == 2


async def test_retention_cleans_only_old_product_events(analytics):
    service, store, *_ = analytics
    async with store.pool.connection() as conn:
        history_before = (await (await conn.execute("SELECT count(*) FROM history_sample")).fetchone())[0]
        await conn.execute(
            "INSERT INTO product_event(id,visitor_hash,occurred_at,page,kind) VALUES(%s,%s,now()-interval '91 days','workbench','page_view')",
            (uuid4(), "a" * 64),
        )
    await service.record(batch())
    async with store.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM product_event")).fetchone())[0] == 1
        assert (await (await conn.execute("SELECT count(*) FROM history_sample")).fetchone())[
            0
        ] == history_before


async def test_disabled_analytics_never_requires_database():
    service = AnalyticsService(None, Settings(_env_file=None, analytics_enabled=False))
    assert await service.record(batch()) == {"enabled": False, "accepted": 0}
    assert await service.summary(7) == {"enabled": False}


async def test_api_rejects_private_payload_cross_origin_and_invalid_window(analytics):
    _, _, source, *_ = analytics
    app = create_app(source.settings, source)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        body = batch().model_dump(mode="json")
        assert (await client.post("/api/v1/analytics/events", json=body)).status_code == 200
        assert (
            await client.post(
                "/api/v1/analytics/events", json=body, headers={"Origin": "https://other.example"}
            )
        ).status_code == 403
        assert (
            await client.post("/api/v1/analytics/events", json={**body, "url": "private"})
        ).status_code == 422
        assert (await client.get("/api/v1/analytics/summary?window=1000d")).status_code == 422
        assert (
            await client.post(
                "/api/v1/analytics/events", content="x" * 4097, headers={"Content-Type": "application/json"}
            )
        ).status_code == 413
        report = (await client.get("/api/v1/analytics/summary?window=1d")).json()
        assert report["pv"] == 1 and len(report["daily"]) == 1


async def test_schema_14_upgrade_additive_and_repeatable(analytics):
    _, store, *_ = analytics
    async with store.pool.connection() as conn:
        before = (await (await conn.execute("SELECT count(*) FROM history_sample")).fetchone())[0]
        await conn.execute("DROP TABLE product_event,product_analytics_metadata")
        await conn.execute("UPDATE history_metadata SET version=14 WHERE id=1")
    await store.initialize()
    await store.initialize()
    async with store.pool.connection() as conn:
        assert (await (await conn.execute("SELECT version FROM history_metadata")).fetchone())[0] == 15
        assert (await (await conn.execute("SELECT count(*) FROM history_sample")).fetchone())[0] == before
