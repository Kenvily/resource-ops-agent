"""Synthetic execution-contract cases; these are not a model capability benchmark."""

import asyncio
import json

import pytest

from app.agents.contracts import TaskContract, provenance, verify_references
from app.agents.runtime import InvestigationRuntime
from app.clients.prometheus import SourceError


class Registry:
    def __init__(self):
        self.evidence, self.cache, self.calls = [], {}, 0
        self.clusters = ["yz"]

    def schemas(self):
        return [
            {
                "function": {
                    "name": "observe",
                    "parameters": {
                        "type": "object",
                        "properties": {"cluster": {"type": "string"}, "segment": {"type": "string"}},
                        "required": ["cluster"],
                    },
                }
            }
        ]

    async def call(self, name, args):
        if args["cluster"] != "yz":
            raise SourceError("scope")
        key = json.dumps(args, sort_keys=True)
        if key in self.cache:
            return self.cache[key]
        self.calls += 1
        evidence = {"id": str(self.calls), "label": "读取统计", "data": {"observed": 1}}
        self.evidence.append(evidence)
        self.cache[key] = evidence
        return evidence


def call(name="observe", arguments=None, id="call"):
    return {
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"cluster": "yz"} if arguments is None else arguments),
        },
    }


class Provider:
    ready = True

    def __init__(self, messages):
        self.responses, self.seen = iter(messages), []

    async def complete(self, messages, _tools):
        self.seen.append(list(messages))
        return next(self.responses)


async def discard(_event):
    pass


async def no_supplement(_session):
    pass


def contract(**changes):
    return TaskContract(
        kind="allocation_change",
        clusters=("yz",),
        max_rounds=6,
        max_tool_calls=8,
        max_calls_per_round=3,
        timeout_seconds=1,
        **changes,
    )


async def run(provider, registry=None, supplement=no_supplement, task=None, emit=discard):
    return await InvestigationRuntime().run(
        provider,
        registry or Registry(),
        task or contract(),
        system="只读",
        context={"question": "核查资源变化"},
        emit=emit,
        supplement=supplement,
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"role": "user"},
        {"role": "assistant", "tool_calls": "invalid"},
        {"role": "assistant", "tool_calls": [call()] * 33},
        {"role": "assistant", "tool_calls": [call(), call()]},
        {"role": "assistant", "tool_calls": [{"type": "shell"}]},
        {"role": "assistant", "tool_calls": [call(name="shell")]},
        {"role": "assistant", "tool_calls": [call(arguments={})]},
        {"role": "assistant", "tool_calls": [call(arguments={"cluster": "zw"})]},
        {"role": "assistant", "tool_calls": [call(arguments={"cluster": "yz", "url": "http://invalid"})]},
        {"role": "assistant", "tool_calls": [call(arguments=["yz"])]},
        {"role": "assistant", "tool_calls": [call(arguments="not_object")]},
        {"role": "assistant", "tool_calls": [call(id="")]},
        {"role": "assistant", "tool_calls": [call(id="x" * 101)]},
        {"role": "assistant", "tool_calls": [call(arguments={"cluster": "yz", "segment": "x" * 4096})]},
    ],
)
async def test_invalid_tool_plans_degrade_without_side_effects(bad):
    registry = Registry()
    result = await run(Provider([bad]), registry)
    assert result.mode == "degraded" and registry.calls == 0
    assert result.stop_reason == "model_limited"


async def test_dynamic_steps_supplement_and_reasoning_are_separate():
    registry = Registry()
    provider = Provider(
        [
            {"role": "assistant", "reasoning_content": "private reasoning", "tool_calls": [call()]},
            {"role": "assistant", "content": "调查完成"},
        ]
    )

    async def supplement(session):
        await session.call("observe", {"cluster": "yz", "segment": "counterexample"})

    result = await run(provider, registry, supplement)
    assert result.mode == "degraded" and result.model_calls == 2
    assert result.steps[0]["actor"] == "model"
    assert result.steps[-1]["actor"] == "verification"
    assert result.supplemental_calls == 1
    assert "private reasoning" not in json.dumps(provider.seen)
    assert "private reasoning" not in json.dumps(result.summary())


async def test_repeated_queries_stop_without_repeated_database_reads():
    registry = Registry()
    provider = Provider([{"role": "assistant", "tool_calls": [call(id=str(i))]} for i in range(6)])
    result = await run(provider, registry)
    assert registry.calls == 1 and result.model_calls == 3
    assert result.mode == "degraded" and result.steps[-1]["cached"]


async def test_deadline_retains_completed_evidence_and_cancels_model():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Blocking:
        ready = True

        async def complete(self, *_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    task = contract().model_copy(update={"timeout_seconds": 0.01})
    result = await run(Blocking(), task=task)
    assert entered.is_set() and cancelled.is_set() and result.stop_reason == "deadline"


async def test_user_cancel_propagates_instead_of_becoming_success():
    entered = asyncio.Event()

    class Blocking:
        ready = True

        async def complete(self, *_):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run(Blocking()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cached_evidence_can_be_verified_at_tool_budget():
    registry = Registry()
    provider = Provider([{"role": "assistant", "tool_calls": [call()]}, {"role": "assistant"}])

    async def supplement(session):
        await session.call("observe", {"cluster": "yz"})

    result = await run(provider, registry, supplement, contract().model_copy(update={"max_tool_calls": 1}))
    assert result.mode == "deepseek" and registry.calls == 1 and result.steps[-1]["cached"]


def test_evidence_identity_is_content_and_scope_bound_and_references_are_checked():
    kwargs = {"cluster": "yz", "start": 60, "end": 120, "units": {"unit": "%"}, "status": "complete"}
    first = provenance("observe", {}, {"value": 1}, **kwargs)
    assert first == provenance("observe", {}, {"value": 1}, **kwargs)
    assert (
        first["fingerprint"]
        != provenance("observe", {}, {"value": 1}, **(kwargs | {"cluster": "zw"}))["fingerprint"]
    )
    assert first["data_as_of"] is None
    with pytest.raises(ValueError):
        verify_references([{"evidence_ids": ["invented"]}], [{"id": "known"}])
    with pytest.raises(ValueError):
        verify_references([], [{"id": "same"}, {"id": "same"}])


def test_verifier_rejects_cross_cluster_citation():
    with pytest.raises(ValueError):
        verify_references([{"cluster": "yz", "evidence_ids": ["e1"]}], [{"id": "e1", "cluster": "zw"}])


async def test_shared_gate_bounds_parallel_investigations_and_releases_after_cancel():
    runtime = InvestigationRuntime()
    active, peak = 0, 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocking:
        ready = True

        async def complete(self, *_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return {"role": "assistant"}
            finally:
                active -= 1

    async def work():
        return await runtime.run(
            Blocking(),
            Registry(),
            contract(),
            system="只读",
            context={},
            emit=discard,
            supplement=no_supplement,
        )

    tasks = [asyncio.create_task(work()) for _ in range(3)]
    await entered.wait()
    assert peak == 2
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    release.set()
    await asyncio.gather(*tasks[1:])
    assert peak == 2 and active == 0 and not runtime.gate.locked()


async def test_token_usage_survives_rejected_model_plan_without_counting_it_as_completed_tools():
    message = {
        "role": "assistant",
        "_usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        "tool_calls": [call(id=str(i)) for i in range(33)],
    }
    registry = Registry()
    result = await run(Provider([message]), registry)
    assert result.mode == "degraded" and registry.calls == 0
    assert result.token_usage["total_tokens"] == 12 and result.failure_code == "per_round_budget"


async def test_large_valid_batch_is_deferred_without_expanding_execution_budget():
    registry = Registry()
    provider = Provider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    call(arguments={"cluster": "yz", "segment": str(i)}, id=str(i)) for i in range(5)
                ],
            },
            {"role": "assistant"},
        ]
    )
    result = await run(provider, registry)
    assert registry.calls == 3 and result.model_calls == 2
    assert len([s for s in result.steps if s.get("evidence_id")]) == 3
    assert result.steps[0]["deferred_calls"] == 2
    replies = [json.loads(m["content"]) for m in provider.seen[-1] if m["role"] == "tool"]
    assert sum(m.get("status") == "not_executed" for m in replies) == 2
    assert len(registry.evidence) == 3


async def test_missing_evidence_gets_one_budgeted_correction_not_unbounded_retries():
    registry = Registry()
    provider = Provider(
        [{"role": "assistant"}, {"role": "assistant", "tool_calls": [call()]}, {"role": "assistant"}]
    )
    result = await run(provider, registry, task=contract(min_evidence=1))
    assert registry.calls == 1 and result.model_calls == 3 and result.mode == "deepseek"
    assert sum(m["role"] == "user" for m in provider.seen[-1]) == 2
    exhausted = await run(
        Provider([{"role": "assistant"}, {"role": "assistant"}]), task=contract(min_evidence=1)
    )
    assert exhausted.model_calls == 2 and exhausted.mode == "degraded"
