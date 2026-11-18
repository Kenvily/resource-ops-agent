import asyncio
import json

import httpx
import pytest
from test_insights import settings

from app.agents.answer import interpret, narrate
from app.agents.models import understand
from app.insights.provider import DeepSeekClient, ModelUnavailable


@pytest.mark.parametrize(
    "question",
    ["两个机房总体服务器配置如何？", "两地机器整体怎么样", "CPU核数和系统内存配置", "GPU型号有哪些？"],
)
def test_broad_configuration_understanding(question):
    intent = understand(question)
    assert "inventory" in intent["metrics"] and intent["direct"]


async def test_semantic_projection_never_sends_raw_question_or_private_name():
    class Parser:
        async def complete(self, messages, tools):
            text = json.dumps(messages, ensure_ascii=False)
            assert "private-service-x" not in text and "10.8.1.2" not in text
            assert "资源" in text and "紧张" not in text  # not in the current public vocabulary
            return {
                "tool_calls": [
                    {
                        "function": {
                            "name": "describe_resource_goal",
                            "arguments": json.dumps(
                                {
                                    "metrics": ["allocation", "gpu_util"],
                                    "operations": ["change"],
                                    "snapshot": False,
                                }
                            ),
                        }
                    }
                ]
            }

    result = await interpret(Parser(), "private-service-x 10.8.1.2 资源是否足够？", [], [])
    assert result["metrics"] == ["allocation", "gpu_util"]


def report():
    return {
        "findings": [
            {"title": "CPU逻辑核数合计 64 核", "level": "fact", "cluster": "yz", "evidence_ids": ["e1"]}
        ],
        "evidence": [{"id": "e1"}],
        "entities": [],
    }


async def test_real_stream_transport_discards_reasoning_and_collects_usage():
    async def handle(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            content='data: {"choices":[{"delta":{"reasoning_content":"private chain", "content":"你好"}}]}\n\ndata: {"choices":[],"usage":{"total_tokens":12}}\n\ndata: [DONE]\n\n',
        )

    provider = DeepSeekClient(
        settings(llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="test"),
        httpx.MockTransport(handle),
    )
    try:
        chunks = [p async for p in provider.stream_text([{"role": "user", "content": "test"}])]
        assert chunks == [{"text": "你好"}, {"usage": {"total_tokens": 12}}]
    finally:
        await provider.aclose()


async def test_incomplete_stream_is_not_a_completed_answer():
    provider = DeepSeekClient(
        settings(llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="test"),
        httpx.MockTransport(lambda _: httpx.Response(200, content='data: {"choices":[]}\n\n')),
    )
    try:
        with pytest.raises(ModelUnavailable):
            _ = [p async for p in provider.stream_text([])]
    finally:
        await provider.aclose()


async def test_narrative_stream_expands_only_verified_fact_slots():
    class Writer:
        async def stream_text(self, messages):
            yield {"text": "**配置概况**\n\n目前可以确认：{{"}
            yield {"text": "f0}}。\n\n"}
            yield {"usage": {"total_tokens": 15}}

    events = []

    async def emit(event):
        events.append(event)

    text, usage = await narrate(Writer(), report(), {}, emit, 2)
    assert "64 核" in text and "#evidence-1" in text
    assert "{{" not in text and usage["total_tokens"] == 15
    assert len(events) == 2 and all(e["type"] == "answer_delta" for e in events)


@pytest.mark.parametrize(
    "text", ["共有999台。{{f0}}", "{{f99}}", "<script>bad</script>{{f0}}", "{{f0}}\n\n{{f0}}"]
)
async def test_unverified_numbers_ids_html_and_repeated_facts_rejected(text):
    class Writer:
        async def stream_text(self, _):
            yield {"text": text}

    async def emit(_):
        pass

    with pytest.raises(ModelUnavailable):
        await narrate(Writer(), report(), {}, emit, 2)


async def test_cancellation_reaches_stream_provider():
    cancelled = asyncio.Event()

    class Writer:
        async def stream_text(self, _):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            yield {}

    async def emit(_):
        pass

    task = asyncio.create_task(narrate(Writer(), report(), {}, emit, 2))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "packet", ["123", '{"choices":[{"delta":{},"finish_reason":"length"}]}', '{"choices":[null]}']
)
async def test_invalid_or_truncated_stream_never_completes(packet):
    provider = DeepSeekClient(
        settings(llm_enabled=True, llm_data_processing_authorized=True, deepseek_api_key="test"),
        httpx.MockTransport(lambda _: httpx.Response(200, content=f"data: {packet}\n\ndata: [DONE]\n\n")),
    )
    try:
        with pytest.raises(ModelUnavailable):
            _ = [p async for p in provider.stream_text([])]
    finally:
        await provider.aclose()
