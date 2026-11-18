import asyncio

# ruff: noqa: F811
import json
from uuid import UUID, uuid4

import httpx
import pytest
from test_analysis import context  # noqa: F401
from test_insights import Offline
from test_resource_statistics import domain  # noqa: F401

from app.agents.models import AgentQuery, understand
from app.agents.service import AgentService
from app.agents.verification import assess
from app.clients.prometheus import SourceError
from app.efficiency.service import EfficiencyService
from app.insights.service import InsightService
from app.investigations.service import InvestigationService
from app.main import create_app


async def discard(_):
    pass


def service_for(domain, provider=None):
    _, source, history, *_ = domain
    records = InvestigationService(history, source.settings)
    insight = InsightService(history, source.settings, provider or Offline())
    return AgentService(
        history, source.settings, insight, EfficiencyService(history, source.settings), records
    )


@pytest.mark.parametrize(
    "question,metric,direct",
    [
        ("当前GPU有多少张？", "allocation", True),
        ("CPU现在是多少？", "cpu_utilization_pct", True),
        ("近三个月CPU曲线为什么只有几天？", "cpu_utilization_pct", False),
        ("实际显存和分配额度为何相反？", "gpu_memory", False),
        ("有多少系统内存？", "memory_used_bytes", True),
    ],
)
def test_local_domain_understanding(question, metric, direct):
    intent = understand(question)
    assert metric in intent["metrics"] and intent["direct"] is direct


async def test_facts_skip_model_and_idempotent_replay_preserves_memory(domain):
    class NoModel:
        ready = True

        async def complete(self, *_):
            raise AssertionError("simple facts must not call LLM")

    service = service_for(domain, NoModel())

    async def forbidden(*_, **__):
        raise AssertionError("agent must not query Prometheus")

    domain[1].range_query = domain[1].instant_query = forbidden
    query = AgentQuery(question="当前GPU有多少张？", cluster="yz")
    tid = uuid4()
    result = await service.run(query, discard, tid)
    assert result["answer_type"] == "facts" and result["model_calls"] == 0
    assert result["tool_calls"] == 1
    assert result["evidence"][0]["data"]["summary"]["gpu_count"] > 0
    replay = await service.run(query, discard, tid)
    assert replay["investigation"]["reused"]
    assert (await service.memory.load(tid))["memory"]["metrics"] == ["allocation"]


async def test_waiting_input_continues_with_inherited_scope(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="帮我看看", cluster="yz", window="1h"), discard)
    assert root["status"] == "waiting_input"
    tid = UUID(root["investigation"]["id"])
    child = await service.run(AgentQuery(question="CPU历史为什么有缺口？", parent_id=tid), discard)
    assert child["memory"]["evidence_refreshed"]
    assert any(e["tool"] == "inspect_host_sync_status" for e in child["evidence"])
    assert all(e.get("cluster") in ("yz", None) for e in child["evidence"])
    detail = await service.records.detail(UUID(child["investigation"]["id"]))
    assert detail["execution"] and detail["kind"] == "resource_investigation"


async def test_continuation_scope_and_four_turn_limit(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="当前GPU多少？", cluster="yz"), discard)
    tid = UUID(root["investigation"]["id"])
    with pytest.raises(SourceError):
        await service.run(AgentQuery(question="CPU怎么样？", cluster="all", parent_id=tid), discard)
    with pytest.raises(SourceError):
        await service.run(AgentQuery(question="看看这个对象", parent_id=tid, entity_ref="invented"), discard)
    for _ in range(3):
        await service.run(AgentQuery(question="当前GPU多少？", parent_id=tid), discard)
    with pytest.raises(SourceError, match="4轮"):
        await service.run(AgentQuery(question="继续看GPU", parent_id=tid), discard)


async def test_scope_contract_change_invalidates_memory(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="当前GPU多少？", cluster="yz"), discard)
    service.settings.insight_allowed_clusters = ["yz"]
    with pytest.raises(SourceError, match="授权范围"):
        await service.memory.load(UUID(root["investigation"]["id"]))


async def test_question_is_not_sent_to_external_model(domain):
    class Model:
        ready = True

        def __init__(self):
            self.messages = []

        async def complete(self, messages, _):
            self.messages.append(json.dumps(messages, ensure_ascii=False))
            return {"role": "assistant"}

    model = Model()
    service = service_for(domain, model)
    result = await service.run(
        AgentQuery(question="private-model-prod-123的CPU历史为什么不完整？", cluster="yz", window="1h"),
        discard,
    )
    assert not model.messages
    assert result["status"] == "waiting_input" and result["entities"]
    assert result["evidence"] and "reasoning_content" not in str(result)


async def test_read_only_refusal_and_unknown_question_do_not_call_tools(domain):
    service = service_for(domain)
    result = await service.run(AgentQuery(question="重启Pod释放资源", cluster="yz"), discard)
    assert result["status"] == "refused" and not result["evidence"]


async def test_schema_13_upgrade_retains_history_and_memory_constraints(domain):
    store, *_ = domain
    async with store.pool.connection() as c:
        await c.execute("UPDATE history_metadata SET version=13")
    await store.initialize()
    await store.initialize()
    async with store.pool.connection() as c:
        assert (await (await c.execute("SELECT version FROM history_metadata")).fetchone())[0] == 15
        assert (await (await c.execute("SELECT count(*) FROM resource_snapshot")).fetchone())[0] > 0


def test_source_loss_is_never_derived_from_database_gaps():
    proof = {
        "id": "e1",
        "tool": "check_host_history_coverage",
        "cluster": "yz",
        "status": "limited",
        "data": {"statistics_complete_observed_cohort_timestamps": 0, "expected_grid_points": 100},
    }
    assert assess("local_history_gap", [proof], "yz")["state"] == "supported"
    assert assess("source_data_loss", [proof], "yz")["state"] == "unknown"


async def test_unified_http_stream_and_same_origin_validation(domain):
    _, source, _, *_ = domain
    app = create_app(source.settings, insight_provider=Offline())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/agents/query", json={"question": "当前GPU多少？", "cluster": "yz"}
        )
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "result" and events[-1]["data"]["model_calls"] == 0
        assert (
            await client.post(
                "/api/v1/agents/query",
                json={"question": "GPU多少？"},
                headers={"origin": "http://foreign.invalid"},
            )
        ).status_code == 403
        assert (
            await client.post("/api/v1/agents/query", json={"question": "GPU多少？", "sql": "bad"})
        ).status_code == 422


async def test_selected_gpu_continuation_never_expands_back_to_cluster(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="当前GPU有多少张？", cluster="all"), discard)
    selected = next(e for e in root["entities"] if e["dimension"] == "gpu" and e["cluster"] == "yz")
    child = await service.run(
        AgentQuery(
            question="当前GPU有多少张？",
            parent_id=UUID(root["investigation"]["id"]),
            entity_ref=selected["id"],
        ),
        discard,
    )
    proof = next(e for e in child["evidence"] if e["tool"] == "get_resource_snapshot")
    assert proof["cluster"] == "yz" and proof["data"]["summary"]["gpu_count"] == 1
    assert len(proof["data"]["devices"]) == 1
    follow = await service.run(
        AgentQuery(question="现在再查一次", parent_id=UUID(child["investigation"]["id"])), discard
    )
    assert follow["answer_type"] == "facts"
    assert (
        next(e for e in follow["evidence"] if e["tool"] == "get_resource_snapshot")["data"]["summary"][
            "gpu_count"
        ]
        == 1
    )


async def test_ambiguous_followup_asks_for_an_entity_instead_of_inventing_one(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="当前GPU有多少张？", cluster="yz"), discard)
    child = await service.run(
        AgentQuery(question="那是否属于低效占用？", parent_id=UUID(root["investigation"]["id"])), discard
    )
    assert child["status"] == "waiting_input" and child["tool_calls"] == 0 and child["entities"]


async def test_parent_scope_cannot_be_changed_through_parallel_turn_reservations(domain):
    service = service_for(domain)
    root = await service.run(AgentQuery(question="当前GPU多少？", cluster="yz"), discard)
    tid = UUID(root["investigation"]["id"])
    results = await asyncio.gather(
        *(service.run(AgentQuery(question="当前GPU多少？", parent_id=tid), discard) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 3


def test_sampling_cannot_refute_statistical_coverage_gap():
    proofs = [
        {
            "id": "statistics",
            "tool": "check_host_history_coverage",
            "cluster": "yz",
            "status": "limited",
            "data": {"statistics_complete_observed_cohort_timestamps": 0, "expected_grid_points": 60},
        },
        {
            "id": "samples",
            "tool": "inspect_host_sync_status",
            "cluster": "yz",
            "status": "complete",
            "data": {"hourly_point_complete": 60, "hourly_expected": 60},
        },
    ]
    assert assess("local_history_gap", proofs, "yz")["state"] == "supported"
    assert assess("hourly_sampling_gap", proofs, "yz")["state"] == "refuted"


def test_unit_and_contributor_conflicts_prevent_positive_attribution():
    root = {
        "id": "root",
        "tool": "compare_cluster_windows",
        "cluster": "yz",
        "status": "complete",
        "data": {
            "units": {"unit_verified": False},
            "changes": [{"capacity_change_raw": -100, "allocated_change_raw": 80}],
        },
    }
    assert assess("capacity_change", [root], "yz")["state"] == "unknown"
    root["data"]["units"]["unit_verified"] = True
    rank = {
        "id": "rank",
        "tool": "rank_allocation_changes",
        "cluster": "yz",
        "status": "complete",
        "data": {"dimension": "namespace", "cohort_allocated_change_raw": 5},
    }
    assert assess("business_allocation_change", [root, rank], "yz")["state"] == "conflicting"


async def test_long_window_does_not_offer_minute_usage_tools_or_infer_sustained_waste(domain):
    service = service_for(domain)
    result = await service.run(
        AgentQuery(question="近三个月GPU低效占用是否需要核查？", cluster="yz", window="3mo"), discard
    )
    assert result["status"] == "limited"
    assert not any(e["tool"] == "find_low_usage_candidates" for e in result["evidence"])
    assert any("分钟数据" in q for q in result["questions"])


async def test_cancelled_parent_keeps_scope_and_rechecks_facts_without_old_entity_claims(domain):
    service = service_for(domain)
    query = AgentQuery(question="当前GPU有多少张？", cluster="yz", window="1h", metrics=["allocation"])
    tid = uuid4()
    await service.records.begin(tid, "resource_investigation", ["yz"], query.model_dump(mode="json"))
    await service.memory.reserve(tid, None, None)
    await service.records.finish(tid, "cancelled", reason="synthetic cancellation")
    result = await service.run(AgentQuery(question="现在再查GPU有多少张？", parent_id=tid), discard)
    assert result["evidence"] and result["memory"]["evidence_refreshed"]
    assert all(e.get("cluster") in ("yz", None) for e in result["evidence"])


async def test_legacy_diagnosis_object_can_start_a_new_scoped_chain(domain):
    from app.insights.models import DiagnosisRequest

    service = service_for(domain)
    gpu = domain[1].fixture["yz"][0]
    key = [gpu["node"], gpu["uuid"] if "uuid" in gpu else gpu["device_uuid"]]
    query = DiagnosisRequest(cluster="yz", window="1h")

    async def legacy(_):
        return {
            "status": "complete",
            "findings": [],
            "evidence": [],
            "entities": [{"id": "legacy-ref", "cluster": "yz", "dimension": "gpu", "key": key}],
        }

    root = await service.records.run("allocation_change", query, discard, legacy)
    child = await service.run(
        AgentQuery(
            question="当前GPU有多少张？", parent_id=UUID(root["investigation"]["id"]), entity_ref="legacy-ref"
        ),
        discard,
    )
    assert child["evidence"][0]["data"]["summary"]["gpu_count"] == 1


async def test_question_time_is_not_silently_replaced_by_default_window(domain):
    service = service_for(domain)
    query, _, _ = await service.prepare(AgentQuery(question="为什么近三个月CPU曲线不完整？", cluster="yz"))
    assert query.window == "3mo"
    with pytest.raises(SourceError, match="时段"):
        await service.prepare(AgentQuery(question="近三个月CPU如何？", cluster="yz", window="1h"))
    with pytest.raises(SourceError, match="绝对日期"):
        await service.prepare(AgentQuery(question="昨天CPU情况？", cluster="yz"))


@pytest.mark.parametrize(
    "question,destination",
    [
        ("评估部署2张GPU每卡20GB", "#planning"),
        ("生成运营报告", "#operations?view=reports"),
        ("GPU利用率低于15%怎么核查？", "#analysis?view=efficiency"),
    ],
)
async def test_simple_professional_tasks_route_without_paying_for_an_agent(domain, question, destination):
    service = service_for(domain)
    result = await service.run(AgentQuery(question=question, cluster="yz"), discard)
    assert result["answer_type"] == "navigation" and result["model_calls"] == result["tool_calls"] == 0
    assert result["next_actions"][0]["href"] == destination


async def test_unified_allocation_reuses_business_drilldown_and_counterchecks(domain):
    import time

    from app.insights.models import DiagnosisRequest
    from app.insights.tools import windows_for

    _, source, history, *_ = domain
    now = int(time.time())
    pair = windows_for(DiagnosisRequest(cluster="yz", window="1h"), now, 90)["full"]
    await history.sync.ingest("yz", 60, pair["baseline"].start, pair["baseline"].end)
    source.fixture["yz"][0]["allocations"][0]["value"] += 100
    await history.sync.ingest("yz", 60, pair["target"].start, pair["target"].end)

    async def forbidden(*args, **kwargs):
        raise AssertionError("source query from investigation")

    source.instant_query = source.range_query = forbidden
    result = await service_for(domain).run(
        AgentQuery(question="分析分配额度变化原因", cluster="yz", window="1h"), discard, clock=now
    )
    assert any(
        e["tool"] == "rank_allocation_changes" and e["data"].get("dimension") == "namespace"
        for e in result["evidence"]
    )
    assert any(e["tool"] == "inspect_resource_entity" for e in result["evidence"])
    assert result["tool_calls"] <= source.settings.insight_max_tool_calls


async def test_composed_registry_default_arguments_reuse_the_correct_proof_not_last_other_tool(domain):
    from app.agents.registry import ResourceRegistry

    service = service_for(domain)
    query, _, _ = await service.prepare(AgentQuery(question="CPU覆盖如何？", cluster="yz", window="1h"))
    registry = ResourceRegistry(service, query, discard)
    first = await registry.call("check_host_history_coverage", {"cluster": "yz"})
    other = await registry.call("inspect_host_sync_status", {"cluster": "yz"})
    replay = await registry.call(
        "check_host_history_coverage",
        {"cluster": "yz", "segment": "full", "host_metric": "cpu_utilization_pct"},
    )
    assert first["id"] != other["id"] and replay["id"] == first["id"]
    assert replay["tool"] == "check_host_history_coverage" and registry.calls == 2
    assert len(registry.evidence) == 2


@pytest.mark.parametrize("question", ["GPU温度是多少？", "服务器有多少CPU核数？", "当前GPU型号有几种？"])
async def test_asset_questions_query_actual_inventory_facts(domain, question):
    result = await service_for(domain).run(AgentQuery(question=question, cluster="yz"), discard)
    assert result["answer_type"] == "facts" and result["evidence"]
    assert all(f.get("metric") == "inventory" for f in result["findings"])


async def test_inventory_uses_both_database_snapshots_and_streams_grounded_answer(domain):
    class Writer:
        ready = True

        async def stream_text(self, _):
            yield {"text": "两地的配置可以分别查看。\n\n{{f0}}\n\n{{f1}}"}

    events = []

    async def emit(event):
        events.append(event)

    result = await service_for(domain, Writer()).run(
        AgentQuery(question="两个机房总体服务器配置如何？", cluster="all"), emit
    )
    assert result["answer_type"] == "facts" and result["model_calls"] == 1
    assert {e["cluster"] for e in result["evidence"]} == {"yz", "zw"}
    assert result["answer_status"] == "complete" and "{{" not in result["answer_markdown"]
    assert any(e["type"] == "answer_delta" for e in events)


async def test_failed_writer_resets_draft_and_keeps_verified_findings(domain):
    from app.insights.provider import ModelUnavailable

    class Writer:
        ready = True

        async def stream_text(self, _):
            yield {"text": "目前配置如下：{{f0}}\n\n"}
            raise ModelUnavailable("upstream private error")

    events = []

    async def emit(event):
        events.append(event)

    result = await service_for(domain, Writer()).run(
        AgentQuery(question="服务器总体配置如何？", cluster="yz"), emit
    )
    assert result["answer_status"] == "fallback" and "answer_markdown" not in result
    assert result["findings"] and any(e["type"] == "answer_reset" for e in events)
    assert "upstream private error" not in json.dumps(result)
