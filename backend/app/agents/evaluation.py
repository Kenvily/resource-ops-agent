"""Versioned synthetic planning benchmark. Scripted scores are protocol checks, never LLM evidence."""

import json
import time
from pathlib import Path

from app.agents.contracts import TaskContract
from app.agents.runtime import InvestigationRuntime
from app.agents.verification import KINDS, assess


def load_cases():
    return json.loads((Path(__file__).parent / "fixtures/benchmark_v1.json").read_text())["cases"]


class ScenarioRegistry:
    def __init__(self, case):
        self.case, self.clusters = case, [case["cluster"]]
        self.evidence, self.cache, self.calls = [], {}, 0

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "读取固定合成场景中的只读证据",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"cluster": {"type": "string", "enum": self.clusters}},
                        "required": ["cluster"],
                    },
                },
            }
            for name in self.case["evidence"]
        ]

    async def call(self, name, args):
        if name not in self.case["evidence"] or args != {"cluster": self.case["cluster"]}:
            raise ValueError("evaluation scope violation")
        if name in self.cache:
            return self.cache[name]
        self.calls += 1
        proof = {
            "id": f"eval-{self.calls}",
            "tool": name,
            "label": name,
            "cluster": self.case["cluster"],
            **self.case["evidence"][name],
        }
        self.evidence.append(proof)
        self.cache[name] = proof
        return proof


class ScriptedProvider:
    ready = True

    async def complete(self, messages, tools):
        done = {json.loads(m["content"])["tool"] for m in messages if m["role"] == "tool"}
        names = [t["function"]["name"] for t in tools if t["function"]["name"] not in done][:3]
        cluster = json.loads(messages[1]["content"])["cluster"]
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call-{len(done)}-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps({"cluster": cluster})},
                }
                for i, name in enumerate(names)
            ],
        }


async def discard(_):
    pass


def score(case, predictions, evidence):
    known = {e["id"] for e in evidence}
    refs = [ref for p in predictions for ref in p.get("evidence_ids", [])]
    observed = {p["hypothesis"]: p["state"] for p in predictions if p.get("hypothesis") in KINDS}
    correct = sum(observed.get(k) == v for k, v in case["expected"].items())
    grounded = all(ref in known for ref in refs)
    unfounded = sum(
        p.get("state") in ("supported", "refuted", "conflicting")
        and (
            p.get("hypothesis") not in KINDS
            or assess(p["hypothesis"], evidence, case["cluster"])["state"] != p["state"]
        )
        for p in predictions
    )
    return {
        "task_success": correct == len(case["expected"]) and grounded,
        "correct_claims": correct,
        "expected_claims": len(case["expected"]),
        "reference_validity": grounded,
        "unfounded_claims": unfounded,
        "missing_or_wrong_states": [k for k, v in case["expected"].items() if observed.get(k) != v],
    }


async def evaluate(case, mode, provider=None):
    began = time.perf_counter()
    reg = ScenarioRegistry(case)
    model_calls = 0
    usage = {}
    steps = []
    execution_stop = None
    execution_failure = None
    if mode in ("rules", "single_shot"):
        # Fixed rule baseline is deliberately strong: same allowed data and budget, all five templates.
        for name in case["evidence"]:
            await reg.call(name, {"cluster": case["cluster"]})
    if mode == "agent":
        execution = await InvestigationRuntime().run(
            provider or ScriptedProvider(),
            reg,
            TaskContract(
                kind="resource_investigation",
                clusters=tuple(reg.clusters),
                max_rounds=6,
                max_tool_calls=8,
                max_calls_per_round=3,
                min_evidence=1,
                timeout_seconds=90,
            ),
            system="只读调查。通过工具检查全部目标假设，尤其是单位、贡献一致性和采样粒度；证据不足时停止，不猜测。",
            context={
                "goal": case["goal"],
                "cluster": case["cluster"],
                "target_hypotheses": list(case["expected"]),
            },
            emit=discard,
            supplement=discard,
        )
        execution_stop, execution_failure = execution.stop_reason, execution.failure_code
        model_calls = execution.model_calls
        steps = execution.steps
        usage = execution.summary().get("token_usage", {})
    predictions = [assess(kind, reg.evidence, case["cluster"]) for kind in case["expected"]]
    if mode == "single_shot" and provider is not None:
        message = await provider.complete(
            [
                {
                    "role": "system",
                    "content": "根据证据一次性判断各目标假设，必须调用submit_verdicts。state只能supported/refuted/conflicting/unknown，引用只能使用现有证据id。数据库缺口不能证明源端数据丢失，未知单位不能判断额度。",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "goal": case["goal"],
                            "cluster": case["cluster"],
                            "targets": list(case["expected"]),
                            "evidence": reg.evidence,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_verdicts",
                        "description": "输出判断和证据引用",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "verdicts": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "hypothesis": {"type": "string", "enum": list(KINDS)},
                                            "state": {
                                                "type": "string",
                                                "enum": ["supported", "refuted", "conflicting", "unknown"],
                                            },
                                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                                        },
                                        "required": ["hypothesis", "state", "evidence_ids"],
                                    },
                                }
                            },
                            "required": ["verdicts"],
                        },
                    },
                }
            ],
        )
        model_calls = 1
        usage = message.get("_usage", {})
        calls = message.get("tool_calls") or []
        predictions = json.loads(calls[0]["function"]["arguments"]).get("verdicts", []) if calls else []
    verifier_predictions = [assess(kind, reg.evidence, case["cluster"]) for kind in case["expected"]]
    return {
        "score_basis": "raw_single_shot"
        if mode == "single_shot" and provider is not None
        else "deterministic_verifier",
        "same_evidence_verifier_score": score(case, verifier_predictions, reg.evidence),
        "case_id": case["id"],
        "split": case["split"],
        "scenario": case["scenario"],
        "mode": mode,
        "provider": "deepseek" if provider else "scripted_protocol",
        **score(case, predictions, reg.evidence),
        "stop_reason": execution_stop,
        "failure_code": execution_failure,
        "tool_calls": reg.calls,
        "model_calls": model_calls,
        "elapsed_ms": round((time.perf_counter() - began) * 1000, 2),
        "token_usage": usage,
        "billing_cost": None,
        "steps": steps,
        "predictions": predictions,
        "evidence": reg.evidence,
    }
