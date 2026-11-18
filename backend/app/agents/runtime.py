"""One bounded model/tool loop for both investigation strategies; no arbitrary SQL or source access."""

import asyncio
import json
import time

from app.agents.contracts import Execution
from app.clients.prometheus import SourceError
from app.insights.provider import ModelUnavailable


class ToolSession:
    def __init__(self, registry, contract, emit, execution):
        self.registry, self.contract, self.emit, self.execution = registry, contract, emit, execution
        self.actor = "model"
        self.attempts = 0
        self.results = {}

    def __getattr__(self, name):
        return getattr(self.registry, name)

    async def call(self, name, arguments):
        # Validate the advertised schema before entering the domain registry. The registry
        # additionally checks typed values, allowed clusters and previously issued entity handles.
        schema = next(
            (s["function"]["parameters"] for s in self.schemas() if s["function"]["name"] == name), None
        )
        if not schema or not isinstance(arguments, dict):
            raise SourceError("工具未授权或参数无效")
        if set(arguments) - set(schema.get("properties", {})) or set(schema.get("required", [])) - set(
            arguments
        ):
            raise SourceError("工具参数不符合契约")
        if self.attempts >= self.contract.max_tool_calls * 2:
            raise SourceError("工具尝试达到预算")
        self.attempts += 1
        if "cluster" in arguments and arguments["cluster"] not in self.contract.clusters:
            raise SourceError("工具集群超出任务授权")
        key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        before = len(self.registry.evidence)
        if key not in self.results and self.registry.calls >= self.contract.max_tool_calls:
            raise SourceError("工具次数达到预算")
        started = time.perf_counter()
        evidence = self.results[key] if key in self.results else await self.registry.call(name, arguments)
        self.results[key] = evidence
        item = evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else evidence
        step = {
            "actor": self.actor,
            "tool": name,
            "evidence_id": item["id"],
            "cached": len(self.registry.evidence) == before,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        self.execution.steps.append(step)
        if self.actor == "verification" and not step["cached"]:
            self.execution.supplemental_calls += 1
        await self.emit(
            {
                "type": "progress",
                "data": {
                    "label": ("证据复核：" if self.actor == "verification" else "模型核查：")
                    + item.get("label", name),
                    **step,
                },
            }
        )
        return evidence


class InvestigationRuntime:
    def __init__(self):
        self.gate = asyncio.Semaphore(2)

    async def run(self, provider, registry, contract, *, system, context, emit, supplement):
        result = Execution(mode="deepseek" if provider.ready else "deterministic")
        try:
            async with asyncio.timeout(contract.timeout_seconds), self.gate:
                if set(registry.clusters) != set(contract.clusters):
                    raise SourceError("执行器与任务授权范围不一致")
                await self._execute(provider, registry, contract, system, context, emit, supplement, result)
        except TimeoutError:
            result.stop_reason = "deadline"
            result.mode = "degraded" if provider.ready else "deterministic"
            result.warnings.append("达到调查时间预算，已保留证据，不补造结论")
        return result

    async def _execute(self, provider, registry, contract, system, context, emit, supplement, result):
        session = ToolSession(registry, contract, emit, result)
        if provider.ready:
            messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            **context,
                            "execution_budget": {
                                "max_calls_per_round": contract.max_calls_per_round,
                                "max_rounds": contract.max_rounds,
                                "max_tool_calls": contract.max_tool_calls,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            stagnant = 0
            requested_missing_evidence = False
            try:
                for round_number in range(1, contract.max_rounds + 1):
                    result.model_calls = round_number
                    registry.model_calls = round_number
                    await emit(
                        {
                            "type": "progress",
                            "data": {
                                "label": "模型正在选择下一步核查",
                                "model_calls": round_number,
                                "actor": "model",
                            },
                        }
                    )
                    message = await provider.complete(messages, registry.schemas())
                    for key, value in (
                        message.get("_usage", {})
                        if isinstance(message, dict) and isinstance(message.get("_usage", {}), dict)
                        else {}
                    ).items():
                        if (
                            key in ("prompt_tokens", "completion_tokens", "total_tokens")
                            and type(value) is int
                            and value >= 0
                        ):
                            result.token_usage[key] = result.token_usage.get(key, 0) + value
                    calls = self.validate_message(message, 32)
                    # Do not retain hidden reasoning or treat free-form model claims as verified findings.
                    messages.append(
                        {k: message[k] for k in ("role", "content", "tool_calls") if k in message}
                    )
                    if not calls:
                        if len(registry.evidence) < contract.min_evidence:
                            if not requested_missing_evidence and round_number < contract.max_rounds:
                                requested_missing_evidence = True
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": "尚无足够查询证据，不能直接形成事实结论。请使用可用只读工具补充证据；最多纠正一次，仍计入轮次预算。",
                                    }
                                )
                                continue
                            raise ModelUnavailable("模型未完成必要证据查询")
                        result.stop_reason = "model_finished"
                        break
                    before = len(registry.evidence)
                    if len(calls) > contract.max_calls_per_round:
                        result.steps.append(
                            {
                                "actor": "model",
                                "tool": "batch_budget",
                                "deferred_calls": len(calls) - contract.max_calls_per_round,
                                "executed": False,
                            }
                        )
                        await emit(
                            {
                                "type": "progress",
                                "data": {
                                    "label": "额外工具请求未执行，保留单轮预算，下一轮可补查",
                                    "actor": "scheduler",
                                    "deferred_calls": len(calls) - contract.max_calls_per_round,
                                },
                            }
                        )
                    for index, call in enumerate(calls):
                        if index >= contract.max_calls_per_round:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": json.dumps(
                                        {
                                            "status": "not_executed",
                                            "reason": "per_round_budget",
                                            "message": "本轮预算已用完，未执行本请求；如仍需要，请在下一轮重新调用",
                                        },
                                        ensure_ascii=False,
                                    ),
                                }
                            )
                            continue
                        function = call["function"]
                        arguments = json.loads(function["arguments"])
                        evidence = await session.call(function["name"], arguments)
                        value = registry.for_model(evidence) if hasattr(registry, "for_model") else evidence
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps(value, ensure_ascii=False, allow_nan=False),
                            }
                        )
                    stagnant = stagnant + 1 if len(registry.evidence) == before else 0
                    if stagnant >= 2:
                        raise ModelUnavailable("重复工具未产生新证据，停止模型循环")
                else:
                    raise ModelUnavailable("模型达到轮次预算")
            except (ModelUnavailable, SourceError, ValueError, KeyError, TypeError) as error:
                result.failure_code = (
                    "per_round_budget"
                    if isinstance(error, ModelUnavailable) and "单轮" in str(error)
                    else "invalid_arguments"
                    if isinstance(error, (ValueError, TypeError))
                    else "model_or_tool_error"
                )
                result.mode = "degraded"
                result.stop_reason = "model_limited"
                result.warnings.append("模型不可用、参数越界或达到预算，已转为有界证据核查")
        else:
            result.warnings.append("DeepSeek 未启用或密钥缺失，当前为确定性核查流程，不是模型诊断")
        session.actor = "verification"
        try:
            await supplement(session)
        except SourceError:
            result.stop_reason = "tool_budget_or_data_limit"
            result.warnings.append("工具预算或数据受限，保留已完成证据")
        if result.supplemental_calls and result.mode == "deepseek":
            result.mode = "degraded"
            result.warnings.append("模型未覆盖全部必要核查，服务端已补充确定性查证")
        await emit(
            {
                "type": "progress",
                "data": {
                    "label": "调查执行结束，正在核验证据引用",
                    "actor": "verification",
                    "stop_reason": result.stop_reason,
                },
            }
        )
        return result

    @staticmethod
    def validate_message(message, maximum):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelUnavailable("模型响应结构无效")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) > maximum:
            raise ModelUnavailable("单轮工具调用超过预算")
        ids = set()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ModelUnavailable("模型工具调用无效")
            key, function = call.get("id"), call.get("function")
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 100
                or key in ids
                or not isinstance(function, dict)
            ):
                raise ModelUnavailable("模型工具调用标识无效")
            ids.add(key)
            if not isinstance(function.get("name"), str):
                raise ModelUnavailable("模型工具名称无效")
            raw = function.get("arguments")
            if not isinstance(raw, str) or len(raw.encode()) > 4096:
                raise ModelUnavailable("模型工具参数超过预算")
        return calls
