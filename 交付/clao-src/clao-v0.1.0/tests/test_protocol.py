from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from closed_loop_agent_orchestrator.protocol import (
    AuditReport,
    AuditRequest,
    Decision,
    PlannerDecision,
)


def test_decision_accepts_only_project_protocol_values() -> None:
    assert [decision.value for decision in Decision] == [
        "PASS",
        "LOCAL_FIX",
        "REPLAN",
        "HUMAN",
    ]
    assert Decision("LOCAL_FIX") is Decision.LOCAL_FIX

    with pytest.raises(ValueError):
        Decision("RETRY")


def test_audit_request_round_trips_exact_wire_fields() -> None:
    request = AuditRequest(
        audit_id="audit-中文-1",
        target_session_id="worker-1",
        task_goal="修复超时处理",
        acceptance_criteria=["超时返回明确错误", "离线测试通过"],
        constraints=["不修改 CLI"],
        evidence=["pytest: one timeout failure"],
    )

    document = request.to_json()

    assert json.loads(document) == {
        "auditId": "audit-中文-1",
        "targetSessionId": "worker-1",
        "taskGoal": "修复超时处理",
        "acceptanceCriteria": ["超时返回明确错误", "离线测试通过"],
        "constraints": ["不修改 CLI"],
        "evidence": ["pytest: one timeout failure"],
    }
    assert AuditRequest.from_json(document) == request


def test_audit_request_requires_at_least_one_acceptance_criterion() -> None:
    with pytest.raises(ValueError, match="at least one item"):
        AuditRequest(
            audit_id="audit-1",
            target_session_id="worker-1",
            task_goal="goal",
            acceptance_criteria=[],
            constraints=[],
            evidence=[],
        )

    with pytest.raises(ValueError, match="at least one item"):
        AuditRequest.from_json(
            json.dumps(
                {
                    "auditId": "audit-1",
                    "targetSessionId": "worker-1",
                    "taskGoal": "goal",
                    "acceptanceCriteria": [],
                    "constraints": [],
                    "evidence": [],
                }
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auditId", " \t"),
        ("targetSessionId", "\r\n"),
        ("taskGoal", "   "),
        ("acceptanceCriteria", [" \t "]),
        ("constraints", ["\n"]),
        ("evidence", ["  "]),
    ],
)
def test_audit_request_rejects_blank_strings(
    field: str, value: object
) -> None:
    payload = {
        "auditId": "audit-1",
        "targetSessionId": "worker-1",
        "taskGoal": "goal",
        "acceptanceCriteria": ["criterion"],
        "constraints": [],
        "evidence": [],
    }
    payload[field] = value

    with pytest.raises(ValueError, match=field):
        AuditRequest.from_json(json.dumps(payload))


def test_audit_request_allows_empty_constraints_and_evidence() -> None:
    request = AuditRequest.from_json(
        json.dumps(
            {
                "auditId": "audit-1",
                "targetSessionId": "worker-1",
                "taskGoal": "goal",
                "acceptanceCriteria": ["criterion"],
                "constraints": [],
                "evidence": [],
            }
        )
    )

    assert request.constraints == []
    assert request.evidence == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.pop("taskGoal"), "missing required field"),
        (lambda payload: payload.update({"extra": True}), "unexpected field"),
        (
            lambda payload: payload.update({"acceptanceCriteria": "pass"}),
            "acceptanceCriteria",
        ),
        (
            lambda payload: payload.update({"constraints": [""]}),
            "constraints",
        ),
        (lambda payload: payload.update({"evidence": [1]}), "evidence"),
    ],
)
def test_audit_request_strictly_validates_json_fields(
    mutate: Callable[[dict[str, object]], object], message: str
) -> None:
    payload = {
        "auditId": "audit-1",
        "targetSessionId": "worker-1",
        "taskGoal": "goal",
        "acceptanceCriteria": ["criterion"],
        "constraints": [],
        "evidence": [],
    }
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        AuditRequest.from_json(json.dumps(payload))


def test_audit_report_serializes_project_wire_fields() -> None:
    report = AuditReport(
        audit_id="audit-中文-1",
        target_session_id="worker-1",
        finding="测试尚未覆盖超时路径",
        evidence=["pytest: 1 failed", "diff: tests unchanged"],
        recommended_decision=Decision.LOCAL_FIX,
    )

    assert json.loads(report.to_json()) == {
        "auditId": "audit-中文-1",
        "targetSessionId": "worker-1",
        "finding": "测试尚未覆盖超时路径",
        "evidence": ["pytest: 1 failed", "diff: tests unchanged"],
        "recommendedDecision": "LOCAL_FIX",
    }


def test_audit_report_round_trips_without_optional_recommendation() -> None:
    report = AuditReport(
        audit_id="audit-2",
        target_session_id="worker-2",
        finding="No acceptance evidence",
        evidence=[],
    )

    document = report.to_json()

    assert "recommendedDecision" not in json.loads(document)
    assert AuditReport.from_json(document) == report


def test_planner_decision_parses_valid_json() -> None:
    decision = PlannerDecision.from_json(
        json.dumps(
            {
                "auditId": "audit-1",
                "decision": "LOCAL_FIX",
                "targetSessionId": "worker-1",
                "instruction": "Add the missing timeout test.",
                "reason": "The implementation direction remains valid.",
            }
        )
    )

    assert decision == PlannerDecision(
        audit_id="audit-1",
        decision=Decision.LOCAL_FIX,
        target_session_id="worker-1",
        instruction="Add the missing timeout test.",
        reason="The implementation direction remains valid.",
    )
    assert PlannerDecision.from_json(decision.to_json()) == decision


@pytest.mark.parametrize("missing", ["auditId", "decision", "targetSessionId"])
def test_planner_decision_rejects_missing_required_fields(missing: str) -> None:
    payload = {
        "auditId": "audit-1",
        "decision": "PASS",
        "targetSessionId": "worker-1",
    }
    del payload[missing]

    with pytest.raises(ValueError, match="missing required field"):
        PlannerDecision.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[]", "must be an object"),
        (
            '{"auditId":1,"decision":"PASS","targetSessionId":"worker-1"}',
            "auditId",
        ),
        (
            '{"auditId":"audit-1","decision":1,"targetSessionId":"worker-1"}',
            "decision",
        ),
        (
            '{"auditId":"audit-1","decision":"PASS","targetSessionId":null}',
            "targetSessionId",
        ),
        (
            '{"auditId":"audit-1","decision":"PASS",'
            '"targetSessionId":"worker-1","instruction":[]}',
            "instruction",
        ),
        (
            '{"auditId":"audit-1","decision":"PASS",'
            '"targetSessionId":"worker-1","reason":false}',
            "reason",
        ),
    ],
)
def test_planner_decision_rejects_wrong_field_types(
    document: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PlannerDecision.from_json(document)


def test_planner_decision_rejects_invalid_decision() -> None:
    document = json.dumps(
        {
            "auditId": "audit-1",
            "decision": "RETRY",
            "targetSessionId": "worker-1",
        }
    )

    with pytest.raises(ValueError, match="PASS, LOCAL_FIX, REPLAN, HUMAN"):
        PlannerDecision.from_json(document)
