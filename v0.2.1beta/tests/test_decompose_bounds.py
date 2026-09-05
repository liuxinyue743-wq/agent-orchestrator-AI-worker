"""Regression: max_subtasks=1 must not produce a contradictory decompose
request (was "2..1", which made every LLM attempt fail schema validation
and forced the mission into HUMAN with an empty error detail)."""
import json

import pytest

from loopcore.planner_adapter import (CodexCliPlannerProvider as P,
                                      PROMPT_DIR)


def _plan_with(n_subs):
    return {
        "mission_id": "M-TEST",
        "strategy": "s",
        "subtasks": [
            {"subtask_id": "M-TEST-S%d" % i,
             "objective": "do thing %d" % i,
             "allowed_paths": ["f%d.py" % i],
             "acceptance_criteria": [{"id": "ac1", "description": "d"}]}
            for i in range(n_subs)
        ],
    }


def test_instruction_exactly_one_when_max_sub_1():
    txt = P._decompose_instruction(1)
    assert "EXACTLY 1" in txt
    assert "2..1" not in txt


def test_instruction_prefers_one_and_allows_two():
    txt = P._decompose_instruction(2)
    assert "1..2" in txt
    assert "Prefer EXACTLY 1" in txt
    assert "same file" in txt
    assert "merge cost" in txt
    assert "2..2" not in txt


def test_validate_accepts_single_subtask_when_max_sub_1():
    ok, msg = P._validate_mission_plan(_plan_with(1), 1)
    assert ok, msg
    ok2, _ = P._validate_mission_plan(_plan_with(2), 1)
    assert not ok2


def test_validate_accepts_one_or_two_and_rejects_more_than_budget():
    ok1, msg1 = P._validate_mission_plan(_plan_with(1), 2)
    assert ok1, msg1
    ok2, msg2 = P._validate_mission_plan(_plan_with(2), 2)
    assert ok2, msg2
    ok3, _ = P._validate_mission_plan(_plan_with(3), 2)
    assert not ok3


@pytest.mark.parametrize("count", [1, 2])
def test_two_worker_candidate_accepts_planner_return_of_one_or_two(
        monkeypatch, count):
    planner = P()
    mission = {
        "mission_id": "M-TEST",
        "budgets": {"max_subtasks": 2},
    }
    monkeypatch.setattr(
        planner, "_call_decompose", lambda *_args: _plan_with(count))

    plan = planner.plan_decompose(mission, "DECOMP-M-TEST")

    assert len(plan.subtasks) == count


def test_two_worker_candidate_rejects_planner_return_above_two(monkeypatch):
    planner = P()
    mission = {
        "mission_id": "M-TEST",
        "budgets": {"max_subtasks": 2},
    }
    monkeypatch.setattr(
        planner, "_call_decompose", lambda *_args: _plan_with(3))
    monkeypatch.setattr("loopcore.planner_adapter.time.sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="subtasks must be a list of 1..2"):
        planner.plan_decompose(mission, "DECOMP-M-TEST")


def test_schema_allows_single_subtask_plan():
    with open(PROMPT_DIR.parent / "schemas" / "mission-plan.schema.json",
              encoding="utf-8") as f:
        schema = json.load(f)
    assert schema["properties"]["subtasks"]["minItems"] == 1
