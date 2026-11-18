"""Small, versioned contracts reused by the existing investigation endpoints."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["allocation_change", "gpu_usage", "resource_investigation"]
    clusters: tuple[Literal["yz", "zw"], ...] = Field(min_length=1, max_length=2)
    max_rounds: int = Field(ge=1, le=20)
    max_tool_calls: int = Field(ge=1, le=100)
    max_calls_per_round: int = Field(ge=1, le=4)
    timeout_seconds: float = Field(gt=0, le=600)
    min_evidence: int = Field(default=0, ge=0)


@dataclass
class Execution:
    mode: str = "deterministic"
    model_calls: int = 0
    stop_reason: str = "model_disabled"
    failure_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    supplemental_calls: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)

    def summary(self):
        return {
            "version": 1,
            "stop_reason": self.stop_reason,
            "failure_code": self.failure_code,
            "model_calls": self.model_calls,
            "supplemental_calls": self.supplemental_calls,
            "steps": self.steps,
            "token_usage": self.token_usage,
        }


def provenance(tool, arguments, data, *, cluster, start, end, units, status):
    # Content-addressed, scoped identity supplements legacy task-local IDs; no hidden model reasoning.
    payload = {
        "tool": tool,
        "arguments": arguments,
        "data": data,
        "cluster": cluster,
        "start": start,
        "end": end,
        "units": units,
        "status": status,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    return {
        "version": 1,
        "fingerprint": digest,
        "source": "postgresql",
        "requested_start": start,
        "requested_end": end,
        "data_as_of": None,
        "units": units,
        "status": status,
        "note": "请求窗口不是已覆盖窗口；实际覆盖见证据数据，未知截至时间不推测",
    }


def verify_references(findings, evidence):
    ids = [e.id if hasattr(e, "id") else e["id"] for e in evidence]
    if len(ids) != len(set(ids)):
        raise ValueError("证据引用重复")
    known = set(ids)
    records = {
        item.id if hasattr(item, "id") else item["id"]: item.model_dump()
        if hasattr(item, "model_dump")
        else item
        for item in evidence
    }
    for finding in findings:
        refs = finding.evidence_ids if hasattr(finding, "evidence_ids") else finding.get("evidence_ids", [])
        if any(ref not in known for ref in refs):
            raise ValueError("结论引用了不存在的证据")
        cluster = (
            getattr(finding, "cluster", None) if hasattr(finding, "evidence_ids") else finding.get("cluster")
        )
        if cluster and any(records[ref].get("cluster") not in (None, cluster) for ref in refs):
            raise ValueError("结论引用了其他集群的证据")
