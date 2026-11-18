# ruff: noqa: F811
"""No enterprise identity is inferred from a gateway transport secret."""

import copy

import httpx
import pytest
from psycopg.errors import ReadOnlySqlTransaction
from pydantic import ValidationError
from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.core.config import Settings
from app.core.telemetry import Telemetry
from app.history.snapshots import SnapshotService
from app.history.store import HistoryStore
from app.main import create_app
from app.validation.production import collect, measure


def production(**overrides):
    values = {
        "_env_file": None,
        "production_mode": True,
        "production_public_origin": "https://localhost",
        "gateway_shared_secret": "x" * 40,
        "history_database_url": "postgresql://mock@127.0.0.1/unused",
        "allowed_hosts": ["localhost", "127.0.0.1"],
        "history_query_fill_chunks": 0,
    }
    return Settings(**(values | overrides))


@pytest.mark.parametrize(
    "change",
    [
        {"production_public_origin": "https://localhost:bad"},
        {"gateway_shared_secret": "bad\n" + "x" * 40},
        {"production_public_origin": "http://localhost"},
        {"production_public_origin": "https://foreign.example"},
        {"gateway_shared_secret": ""},
        {"history_database_url": ""},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["test"]},
        {"investigation_feedback_enabled": True},
        {"operations_workspace_enabled": True},
        {"history_query_fill_chunks": 1},
        {"insight_allowed_clusters": ["yz"]},
    ],
)
def test_production_configuration_fails_closed(change):
    with pytest.raises(ValidationError):
        production(**change)


async def test_gateway_covers_site_docs_and_streams_before_app_state_or_queries():
    app = create_app(production())
    # No lifespan: blocked requests must not access pools, sources, model or telemetry.
    for peer in ("127.0.0.1", "203.0.113.7"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url="http://localhost"
        ) as c:
            for path in (
                "/",
                "/docs",
                "/openapi.json",
                "/api/health",
                "/api/v1/insights/diagnose",
                "/api/v1/efficiency/investigate",
            ):
                r = await c.get(path)
                assert r.status_code == 403 and "x" * 40 not in r.text
            if peer != "127.0.0.1":
                assert (
                    await c.get("/api/health", headers={"X-ROC-Gateway-Token": "x" * 40})
                ).status_code == 403
            else:
                r = await c.post(
                    "/api/v1/operations/workspace/assessments", headers={"X-ROC-Gateway-Token": "x" * 40}
                )
                assert r.status_code == 403
                r = await c.post(
                    "/api/v1/investigations/demo/feedback", headers={"X-ROC-Gateway-Token": "x" * 40}
                )
                assert r.status_code == 403


async def test_gateway_secret_is_not_replaceable_by_forwarded_identity():
    app = create_app(production())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
    ) as c:
        r = await c.get("/docs", headers={"X-Forwarded-User": "admin", "X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


async def test_production_snapshot_reads_do_not_sync_even_on_refresh(domain):
    store, source, history, _, _ = domain
    settings = copy.copy(source.settings)
    settings.production_mode = True
    snapshots = SnapshotService(history.resources, store)
    snapshots.settings = settings

    async def forbidden(*args, **kwargs):
        raise AssertionError("read requested synchronization")

    snapshots.synchronize = forbidden
    row = await snapshots.cluster("yz", __import__("time").time(), True)
    assert row.storage_backend == "postgresql"


async def test_release_evidence_is_database_readonly_and_sanitized(domain):
    _store, source, _, _, _ = domain
    readonly = HistoryStore(source.settings, read_only=True)
    await readonly.open()
    try:
        with pytest.raises(ReadOnlySqlTransaction):
            async with readonly.pool.connection() as c:
                await c.execute("DELETE FROM resource_snapshot")
        with pytest.raises(Exception, match="只读验收"):
            await readonly.initialize()
    finally:
        await readonly.close()
    result = await collect(source.settings, runs=1, concurrency=1)
    assert result["transaction_read_only"] == "on" and result["schema_version"] == 15
    assert result["external_calls"] == 0 and result["release_status"] == "pending_enterprise_acceptance"
    assert len(result["gpu_utilization_coverage"]) == 12
    import json

    output = json.dumps(result)
    assert "device_uuid" not in output and "namespace_name" not in output
    assert source.settings.history_database_url.get_secret_value() not in output
    assert all(r["errors"] == 0 for r in result["benchmarks"])


async def test_benchmark_errors_are_counted_without_private_details():
    async def bad():
        raise ValueError("private labels")

    result = await measure(bad, 3, 2)
    assert result["errors"] == 3 and "private" not in str(result)


async def test_valid_local_gateway_reaches_protected_docs_with_safe_headers():
    app = create_app(production())
    app.state.telemetry = Telemetry()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
    ) as c:
        r = await c.get("/docs", headers={"X-ROC-Gateway-Token": "x" * 40})
        assert r.status_code == 200
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-frame-options"] == "DENY"
        assert "x" * 40 not in r.text
        assert (await c.get("/docs", headers={"X-ROC-Gateway-Token": "x" * 39})).status_code == 403


async def test_gateway_fixed_https_origin_supports_same_origin_posts():
    from fastapi import Request

    from app.resources.models import StatisticsRequest
    from app.resources.routes import body

    app = create_app(production())
    app.state.telemetry = Telemetry()

    @app.post("/api/probe")
    async def probe(request: Request):
        return (await body(request, StatisticsRequest)).model_dump()

    app.router.routes.insert(0, app.router.routes.pop())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
    ) as c:
        headers = {"X-ROC-Gateway-Token": "x" * 40, "Origin": "https://localhost"}
        assert (await c.post("/api/probe", headers=headers, json={"cluster": "all"})).status_code == 200
        assert (
            await c.post(
                "/api/probe", headers=headers | {"Origin": "https://foreign.example"}, json={"cluster": "all"}
            )
        ).status_code == 403


async def test_readiness_only_reads_persisted_facts_and_rejects_stale(domain):
    import time

    from app.validation.readiness import readiness

    store, source, history, _, _ = domain

    async def forbidden(*a, **kw):
        raise AssertionError("readiness called Prometheus")

    source.range_query = forbidden
    result = await readiness(history, source.settings)
    assert result["ready"] and result["database"] == "available"
    async with store.pool.connection() as c:
        await c.execute(
            "UPDATE resource_snapshot SET payload=jsonb_set(payload,'{fetched_at}',to_jsonb(%s::float8))",
            (time.time() - 3600,),
        )
    assert not (await readiness(history, source.settings))["ready"]
    assert not (await readiness(None, source.settings))["ready"]


def test_invalid_deployment_configuration_does_not_echo_secret_inputs():
    with pytest.raises(ValidationError) as caught:
        production(production_public_origin="http://localhost", deepseek_api_key="private-model-secret")
    assert "x" * 40 not in str(caught.value)
    assert "private-model-secret" not in str(caught.value)


async def test_production_history_missing_reads_and_all_start_never_fill_or_discover(domain):
    import time

    from app.clients.prometheus import SourceError
    from app.history.service import HistoryService
    from app.services.resources import ResourceService

    _store, source, _, _, _ = domain
    settings = source.settings.model_copy(update={"production_mode": True, "history_query_fill_chunks": 0})
    readonly = HistoryStore(settings, read_only=True)
    await readonly.open()
    history = HistoryService(ResourceService(source, settings), readonly)

    async def forbidden(*a, **kw):
        raise AssertionError("query attempted discovery, guard write or upstream ingestion")

    readonly.claim_fill = forbidden
    history.sync.ingest = forbidden
    history.sync.discover_start = forbidden
    try:
        end = (int(time.time()) - 3600) // 60 * 60
        await history.query("yz", 60, end - 60, end, 60)
        # No stored source discovery is a controlled unavailable result, not a source probe.
        try:
            await history.earliest("yz")
        except SourceError:
            pass
    finally:
        await readonly.close()
