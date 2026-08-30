"""Regression: max_subtasks=1 must not produce a contradictory decompose
request (was "2..1", which made every LLM attempt fail schema validation
and forced the mission into HUMAN with an empty error detail)."""
import json

from loopcore.planner_adapter import (AOOrchestratorPlannerProvider as P,
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


def test_instruction_range_when_max_sub_above_1():
    assert "2..3" in P._decompose_instruction(3)


def test_validate_accepts_single_subtask_when_max_sub_1():
    ok, msg = P._validate_mission_plan(_plan_with(1), 1)
    assert ok, msg
    ok2, _ = P._validate_mission_plan(_plan_with(2), 1)
    assert not ok2


def test_validate_still_requires_two_plus_when_max_sub_above_1():
    ok, _ = P._validate_mission_plan(_plan_with(1), 3)
    assert not ok
    ok2, msg2 = P._validate_mission_plan(_plan_with(3), 3)
    assert ok2, msg2


def test_schema_allows_single_subtask_plan():
    with open(PROMPT_DIR.parent / "schemas" / "mission-plan.schema.json",
              encoding="utf-8") as f:
        schema = json.load(f)
    assert schema["properties"]["subtasks"]["minItems"] == 1
