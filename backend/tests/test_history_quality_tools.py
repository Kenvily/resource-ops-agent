# ruff: noqa: F811
"""Observed DB coverage does not prove source retention or full-fleet coverage."""

import pytest
from test_analysis import context  # noqa: F401
from test_resource_statistics import domain  # noqa: F401

from app.clients.prometheus import SourceError
from app.insights.models import DiagnosisRequest
from app.insights.tools import ToolRegistry


async def discard(_event):
    pass


async def test_host_quality_tools_distinguish_hourly_sampling_and_full_statistics_without_source_calls(
    domain,
):
    store, source, history, *_ = domain
    registry = ToolRegistry(history, DiagnosisRequest(cluster="yz", window="1mo"), source.settings, discard)
    at = registry.windows["full"]["baseline"].start
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE host_metric_bucket,host_history_point,host_point_coverage,host_history_coverage"
        )
        await conn.execute(
            "INSERT INTO host_metric_bucket VALUES('yz','mock-private-node','cpu_utilization_pct',3600,%s,600,5,15,60)",
            (at,),
        )
        await conn.execute(
            "INSERT INTO host_history_point VALUES('yz','mock-private-node','cpu_utilization_pct',%s,0)",
            (at,),
        )
        await conn.execute("INSERT INTO host_point_coverage(cluster,ts,complete) VALUES('yz',%s,true)", (at,))

    async def forbidden(*args, **kwargs):
        raise AssertionError("quality tools must not query Prometheus")

    source.range_query = source.instant_query = forbidden
    proof = await registry.call(
        "check_host_history_coverage", {"cluster": "yz", "host_metric": "cpu_utilization_pct"}
    )
    assert proof.data["statistics_complete_observed_cohort_timestamps"] == 1
    assert proof.data["hourly_instant_observed_timestamps"] == 1
    assert proof.data["expected_grid_points"] > 1
    assert proof.provenance["units"]["unit"] == "%"
    assert proof.provenance["source"] == "postgresql"
    assert proof.status == "limited" and "mock-private-node" not in proof.model_dump_json()
    same = await registry.call(
        "check_host_history_coverage", {"cluster": "yz", "host_metric": "cpu_utilization_pct"}
    )
    assert same.id == proof.id and registry.calls == 1
    sync = await registry.call("inspect_host_sync_status", {"cluster": "yz"})
    assert sync.data["hourly_point_complete"] == 1
    assert sync.data["source_retention_verified"] is False
    assert sync.data["hourly_expected"] > sync.data["hourly_point_complete"]


@pytest.mark.parametrize(
    "args",
    [
        {"cluster": "zw"},
        {"cluster": "yz", "host_metric": "arbitrary_metric"},
        {"cluster": "yz", "sql": "SELECT *"},
        {"cluster": "yz", "url": "http://invalid"},
    ],
)
async def test_quality_tools_reject_cross_scope_and_untyped_queries(domain, args):
    _, source, history, *_ = domain
    registry = ToolRegistry(history, DiagnosisRequest(cluster="yz"), source.settings, discard)
    with pytest.raises(SourceError):
        await registry.call("check_host_history_coverage", args)


async def test_both_live_paths_really_share_the_same_runtime_gate(domain):
    from test_insights import Offline

    from app.efficiency.agent import EfficiencyAgent
    from app.efficiency.service import EfficiencyService
    from app.insights.service import InsightService

    _, source, history, *_ = domain
    insight = InsightService(history, source.settings, Offline())
    usage = EfficiencyAgent(EfficiencyService(history, source.settings), insight.provider, insight.runtime)
    assert insight.runtime is usage.runtime
    assert insight.runtime.gate is usage.runtime.gate


async def test_existing_diagnosis_delivers_cpu_gap_evidence_and_does_not_claim_source_loss(domain):
    from test_insights import Offline

    from app.insights.service import InsightService

    store, source, history, *_ = domain
    async with store.pool.connection() as conn:
        await conn.execute(
            "TRUNCATE host_metric_bucket,host_history_point,host_point_coverage,host_history_coverage"
        )

    async def forbidden(*args, **kwargs):
        raise AssertionError("investigation must be database-only")

    source.range_query = source.instant_query = forbidden
    from uuid import UUID

    from app.insights.models import DiagnosisResult
    from app.investigations.service import InvestigationService

    query = DiagnosisRequest(cluster="yz", window="1h", question="为什么CPU历史曲线有缺口，是否尚未回填？")
    records = InvestigationService(history, source.settings)

    async def execute(emit):
        return await InsightService(history, source.settings, Offline()).run(query, emit)

    raw = await records.run("allocation_change", query, discard, execute)
    result = DiagnosisResult.model_validate(raw)
    coverage = next(e for e in result.evidence if e.tool == "check_host_history_coverage")
    sync = next(e for e in result.evidence if e.tool == "inspect_host_sync_status")
    assert coverage.data["statistics_observed_timestamps"] == 0
    assert sync.data["source_retention_verified"] is False
    finding = next(f for f in result.findings if "CPU" in f.title)
    assert finding.kind == "insufficient" and coverage.id in finding.evidence_ids
    assert any("不能证明Prometheus无数据" in text for text in finding.limitations)
    assert result.execution["supplemental_calls"] > 0
    assert all(s["actor"] == "verification" for s in result.execution["steps"])

    saved = await records.detail(UUID(raw["investigation"]["id"]))
    assert saved["execution"] == result.execution
    assert saved["events"] and any(e["data"].get("actor") == "verification" for e in saved["events"])
    assert all(e["provenance"]["fingerprint"] for e in saved["evidence"])
    replay = await records.run(
        "allocation_change", query, discard, forbidden, UUID(raw["investigation"]["id"])
    )
    assert replay["investigation"]["reused"] and replay["execution"] == result.execution
