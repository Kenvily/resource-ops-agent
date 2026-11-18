#!/usr/bin/env python3
"""Three-arm synthetic Agent evaluation. Never queries Live PostgreSQL or Prometheus."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.agents.evaluation import evaluate, load_cases
from app.core.config import Settings
from app.insights.provider import DeepSeekClient


async def main(args):
    cases = [
        c for c in load_cases() if args.split == "all" or c["split"] == args.split
    ][args.offset : args.offset + args.limit]
    if not cases:
        raise ValueError("没有匹配的冻结任务")
    provider = DeepSeekClient(Settings()) if args.provider == "deepseek" else None
    if provider and not provider.ready:
        raise ValueError("DeepSeek配置或数据处理授权未就绪")
    rows = []
    try:
        for case in cases:
            for mode in ("rules", "single_shot", "agent"):
                try:
                    row = await evaluate(
                        case, mode, provider if mode != "rules" else None
                    )
                except Exception:  # noqa: BLE001 - keep provider errors private
                    row = {
                        "case_id": case["id"],
                        "mode": mode,
                        "error": "模型或评测失败；未生成分数",
                    }
                rows.append(row)
                print(
                    json.dumps(
                        {
                            k: v
                            for k, v in row.items()
                            if k not in ("evidence", "predictions", "steps")
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        if provider:
            await provider.aclose()
    result = {
        "dataset": "synthetic_v1",
        "provider": args.provider,
        "production_validation": False,
        "agent_includes_rule_supplement": False,
        "limitation": "scripted仅验证协议；少量DeepSeek结果不能作为生产或完整泛化结论。合成工具延迟不代表数据库性能。",
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if any("error" in row for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", choices=["scripted", "deepseek"], default="scripted")
    p.add_argument("--split", choices=["all", "development", "holdout"], default="all")
    p.add_argument("--offset", type=int, choices=range(24), default=0)
    p.add_argument("--limit", type=int, choices=range(1, 25), default=24)
    p.add_argument(
        "--output", type=Path, default=Path(".local/validation/agent-evaluation.json")
    )
    try:
        asyncio.run(main(p.parse_args()))
    except Exception:  # noqa: BLE001 - sanitized CLI boundary
        print(
            "Agent评测未完成，请检查配置或运行环境；未输出密钥及上游响应。",
            file=sys.stderr,
        )
        raise SystemExit(1)
