"""Frozen synthetic cases validate the evaluator; scripted runs are not LLM capability scores."""

import pytest

from app.agents.evaluation import evaluate, load_cases


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
async def test_three_arm_offline_protocol(case):
    for mode in ("rules", "single_shot", "agent"):
        row = await evaluate(case, mode)
        assert row["task_success"] and row["reference_validity"], row
        assert row["provider"] == "scripted_protocol" and row["billing_cost"] is None
        assert row["tool_calls"] <= 8


def test_holdout_is_frozen_by_new_scenario_combinations():
    cases = load_cases()
    assert len(cases) == 24 and all(c["synthetic"] for c in cases)
    development = {c["scenario"] for c in cases if c["split"] == "development"}
    holdout = {c["scenario"] for c in cases if c["split"] == "holdout"}
    assert len(holdout) == 8 and not development & holdout
