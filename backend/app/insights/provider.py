"""DeepSeek transport: fixed configured origin, bounded bodies, no response/credential logging."""

import asyncio
import json

import httpx


class ModelUnavailable(Exception):
    pass


class DeepSeekClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=settings.deepseek_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def ready(self):
        return (
            self.settings.llm_enabled
            and self.settings.llm_data_processing_authorized
            and bool(self.settings.deepseek_api_key.get_secret_value())
        )

    async def complete(self, messages, tools):
        if not self.ready:
            raise ModelUnavailable("DeepSeek 未配置或尚未启用")
        body = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": 1536,
            "thinking": {"type": "disabled"},
        }
        if len(json.dumps(body, ensure_ascii=False).encode()) > 100000:
            raise ModelUnavailable("模型上下文达到预算")
        try:
            async with asyncio.timeout(self.settings.deepseek_timeout_seconds):
                async with self.http.stream(
                    "POST",
                    self.settings.deepseek_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.deepseek_api_key.get_secret_value()},
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        reasons = {
                            401: "鉴权失败，请检查密钥",
                            402: "账户额度不足",
                            429: "请求限流，请稍后重试",
                        }
                        raise ModelUnavailable(
                            "DeepSeek "
                            + reasons.get(response.status_code, "服务暂不可用")
                            + f"（HTTP {response.status_code}）"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 128000:
                            raise ModelUnavailable("模型响应超过预算")
                payload = json.loads(data)
                message = payload["choices"][0]["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError()
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > 32:
                    raise ValueError()
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or call.get("type") != "function"
                        or not isinstance(call.get("id"), str)
                        or len(call["id"]) > 100
                        or not isinstance(call.get("function"), dict)
                    ):
                        raise ValueError()
                result = {
                    k: message[k]
                    for k in ("role", "content", "tool_calls", "reasoning_content")
                    if k in message
                }
                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    result["_usage"] = {
                        k: v
                        for k, v in usage.items()
                        if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        and type(v) is int
                        and v >= 0
                    }
                return result
        except ModelUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            raise ModelUnavailable("DeepSeek 请求失败、超时或返回无效结构") from None

    async def stream_text(self, messages):
        """Bounded real SSE transport; cancellation closes the upstream connection."""
        if not self.ready:
            raise ModelUnavailable("DeepSeek 未配置或尚未启用")
        body = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 1536,
            "thinking": {"type": "disabled"},
        }
        if len(json.dumps(body, ensure_ascii=False).encode()) > 100000:
            raise ModelUnavailable("模型上下文达到预算")
        try:
            async with asyncio.timeout(self.settings.deepseek_timeout_seconds):
                async with self.http.stream(
                    "POST",
                    self.settings.deepseek_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.deepseek_api_key.get_secret_value()},
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        raise ModelUnavailable("DeepSeek 流式回答暂不可用")
                    buffer = bytearray()
                    received = 0
                    finished = False
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > 128000:
                            raise ModelUnavailable("模型响应超过预算")
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            if not line.startswith(b"data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == b"[DONE]":
                                finished = True
                                break
                            packet = json.loads(raw)
                            if not isinstance(packet, dict) or not isinstance(
                                packet.get("choices", []), list
                            ):
                                raise TypeError()
                            usage = packet.get("usage")
                            if isinstance(usage, dict):
                                yield {
                                    "usage": {
                                        k: v
                                        for k, v in usage.items()
                                        if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                        and type(v) is int
                                        and v >= 0
                                    }
                                }
                            for choice in packet.get("choices", []):
                                if not isinstance(choice, dict) or not isinstance(
                                    choice.get("delta", {}), dict
                                ):
                                    raise TypeError()
                                if choice.get("finish_reason") not in (None, "stop"):
                                    raise ModelUnavailable("模型回答未正常结束")
                                content = choice.get("delta", {}).get("content")
                                if isinstance(content, str) and content:
                                    yield {"text": content}
                        if finished:
                            break
                    if not finished:
                        raise ModelUnavailable("模型流式回答中断")
        except ModelUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, UnicodeError):
            raise ModelUnavailable("模型流式回答未完成") from None

    async def aclose(self):
        await self.http.aclose()
