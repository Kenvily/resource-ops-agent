"""Quota screening: deterministic units, grouping, quality and database-only evidence."""

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_insights import history as assessment_history  # noqa: F401
from test_insights import settings

from app.clients.prometheus import SourceError
from app.insights.assessment import AssessmentRequest, AssessmentService, runtime_label
from app.main import create_app
from app.models.resource import ClusterSummary, GpuResource, Units


class Connection:
    async def execute(self, _sql):
        pass


class Store:
    pool = None

    def __init__(self):
        self.pool = self

    @asynccontextmanager
    async def connection(self):
        yield Connection()

    async def snapshot(self, cid, conn):
        return cid


def screening(status="healthy", missing=False):
    config = settings()
    now = time.time()

    def device(node, uuid, spare):
        return GpuResource(
            cluster="yz",
            node=node,
            device_uuid=uuid,
            sampled_at=now,
            capacity_raw=80_000,
            allocated_raw=80_000 - spare,
        )

    devices = [device("a", "1", 30_000), device("a", "2", 25_000), device("b", "3", 70_000)]

    def present(cid, at, row, synchronized):
        return ClusterSummary(id=cid, label=cid, status=status, fetched_at=now, gpus=devices)

    async def gpu_history(cid, window, node):
        return SimpleNamespace(
            truncated=False,
            changes=[
                SimpleNamespace(
                    entity=(g.node, g.device_uuid),
                    target=SimpleNamespace(
                        complete=not missing,
                        unallocated_min_raw=22_000,
                        observed_points=0 if missing else window.expected_points,
                        expected_points=window.expected_points,
                    ),
                )
                for g in devices
            ],
        )

    history = SimpleNamespace(
        store=Store(),
        analysis=SimpleNamespace(gpu_history=gpu_history),
        resources=SimpleNamespace(units=lambda: Units(capacity="MB", allocation="MB", unit_verified=True)),
    )
    return AssessmentService(SimpleNamespace(present=present), history, config), devices


@pytest.mark.parametrize("unit,value,expected", [("MB", 1000, 1e9), ("GB", 1, 1e9), ("GiB", 1, 1073741824)])
def test_units(unit, value, expected):
    assert AssessmentRequest(memory_unit=unit, memory_per_gpu=value).required_bytes == expected


@pytest.mark.parametrize(
    "values",
    [
        {"gpu_count": 0},
        {"gpu_count": 2.5},
        {"memory_per_gpu": float("nan")},
        {"memory_unit": "TB"},
        {"duration_hours": 0},
        {"url": "http://bad"},
    ],
)
def test_invalid_constraints(values):
    with pytest.raises(ValidationError):
        AssessmentRequest(**values)


async def test_grouping_and_historical_risk():
    service, _ = screening()
    report = await service.evaluate(AssessmentRequest(cluster="yz"))
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["nodes"] == ["a"]
    assert report["candidates"][0]["risk"] == "within_observed"
    report = await service.evaluate(AssessmentRequest(cluster="yz", memory_per_gpu=24, same_node=False))
    assert report["candidates"][0]["risk"] == "historical_shortfall"
    assert len(report["candidates"][0]["gpus"]) == 2
    assert report["required_bytes_per_gpu"] == 24e9


async def test_unknown_is_not_low_risk():
    service, _ = screening(missing=True)
    report = await service.evaluate(AssessmentRequest(cluster="yz"))
    assert report["candidates"][0]["risk"] == "unknown"
    assert report["candidates"][0]["gpus"][0]["historical_min_bytes"] is None


@pytest.mark.parametrize("status", ["stale", "degraded", "unavailable", "empty"])
async def test_bad_snapshot_excluded(status):
    service, _ = screening(status=status)
    assert not (await service.evaluate(AssessmentRequest(cluster="yz")))["candidates"]


async def test_per_card_not_total_and_node_constraint():
    service, _ = screening()
    report = await service.evaluate(AssessmentRequest(cluster="yz", memory_per_gpu=40))
    assert report["clusters"][0]["eligible_gpu_count"] == 1
    assert not report["candidates"]
    report = await service.evaluate(AssessmentRequest(cluster="yz", node="b"))
    assert not report["candidates"]


async def test_duplicate_device_stops_and_stale_device_excluded():
    service, devices = screening()
    devices[0].sampled_at -= 1000
    report = await service.evaluate(AssessmentRequest(cluster="yz"))
    assert report["clusters"][0]["excluded_gpu_count"] == 1
    assert not report["candidates"]
    devices.append(devices[1])
    with pytest.raises(SourceError):
        await service.evaluate(AssessmentRequest(cluster="yz"))


async def test_api_guards_and_missing_database():
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        assert (await client.post("/api/v1/insights/assess", json={})).status_code == 503
        assert (await client.post("/api/v1/insights/assess", json={"gpu_count": 0})).status_code == 422
        assert (
            await client.post("/api/v1/insights/assess", json={}, headers={"Origin": "https://bad.example"})
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/insights/assess", content=b"x" * 4097, headers={"Content-Type": "application/json"}
            )
        ).status_code == 413


async def test_real_database_evidence_no_prometheus(assessment_history):  # noqa: F811
    from app.history.snapshots import SnapshotService

    db, source, config, now, _pair = assessment_history
    end = int(now - 120) // 60 * 60
    for start in range(end - 3600, end + 1, 30 * 60):
        await db.sync.ingest("yz", 60, start, min(start + 29 * 60, end))
    snapshots = SnapshotService(db.resources, db.store)
    await snapshots.synchronize("yz")

    async def forbidden(*args, **kwargs):
        raise AssertionError("assessment must not query Prometheus")

    source.instant_query = forbidden
    source.range_query = forbidden
    service = AssessmentService(snapshots, db, config)
    report = await service.evaluate(
        AssessmentRequest(cluster="yz", gpu_count=1, memory_per_gpu=1, memory_unit="MB", history_hours=1), now
    )
    assert report["candidates"]
    assert report["candidates"][0]["risk"] == "within_observed"
    gpu = report["candidates"][0]["gpus"][0]
    assert gpu["historical_min_bytes"] == gpu["unallocated_bytes"]
    assert gpu["coverage"] == 1


@pytest.mark.parametrize(
    "hours,label",
    [(1, "1 小时"), (48, "2 天"), (336, "2 周"), (1440, "2 个月（每月按 30 天）"), (8760, "365 天")],
)
def test_runtime_units_and_extended_horizon(hours, label):
    assert AssessmentRequest(duration_hours=hours).duration_hours == hours
    assert runtime_label(hours) == label


@pytest.mark.parametrize("hours", [0.5, 8761])
def test_runtime_bounds(hours):
    with pytest.raises(ValidationError):
        AssessmentRequest(duration_hours=hours)
