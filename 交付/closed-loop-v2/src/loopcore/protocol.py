"""Project-owned JSON protocol exchanged through AO Chat messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Decision(str, Enum):
    """The only decisions an Auditor or Planner may report."""

    PASS = "PASS"
    LOCAL_FIX = "LOCAL_FIX"
    REPLAN = "REPLAN"
    HUMAN = "HUMAN"


@dataclass(frozen=True)
class AuditRequest:
    """Task contract and evidence sent to the read-only Auditor."""

    audit_id: str
    target_session_id: str
    task_goal: str
    acceptance_criteria: list[str]
    constraints: list[str]
    evidence: list[str]

    def __post_init__(self) -> None:
        _validate_non_blank_string(self.audit_id, "audit_id")
        _validate_non_blank_string(self.target_session_id, "target_session_id")
        _validate_non_blank_string(self.task_goal, "task_goal")
        _validate_audit_request_string_list(
            self.acceptance_criteria,
            "acceptance_criteria",
            require_item=True,
        )
        _validate_audit_request_string_list(self.constraints, "constraints")
        _validate_audit_request_string_list(self.evidence, "evidence")

    def to_json(self) -> str:
        """Serialize this request using the project protocol's wire fields."""

        return json.dumps(
            {
                "auditId": self.audit_id,
                "targetSessionId": self.target_session_id,
                "taskGoal": self.task_goal,
                "acceptanceCriteria": self.acceptance_criteria,
                "constraints": self.constraints,
                "evidence": self.evidence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, document: str) -> AuditRequest:
        """Parse one AuditRequest with an exact, required field set."""

        payload = _parse_json_object(document, "AuditRequest")
        _validate_exact_fields(
            payload,
            {
                "auditId",
                "targetSessionId",
                "taskGoal",
                "acceptanceCriteria",
                "constraints",
                "evidence",
            },
            "AuditRequest",
        )
        return cls(
            audit_id=_required_non_blank_string(payload, "auditId"),
            target_session_id=_required_non_blank_string(
                payload, "targetSessionId"
            ),
            task_goal=_required_non_blank_string(payload, "taskGoal"),
            acceptance_criteria=_required_audit_request_string_list(
                payload, "acceptanceCriteria", require_item=True
            ),
            constraints=_required_audit_request_string_list(
                payload, "constraints"
            ),
            evidence=_required_audit_request_string_list(payload, "evidence"),
        )


@dataclass(frozen=True)
class AuditReport:
    """Minimal evidence package sent to the project Planner."""

    audit_id: str
    target_session_id: str
    finding: str
    evidence: list[str]
    recommended_decision: Decision | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.audit_id, "audit_id")
        _validate_non_empty_string(self.target_session_id, "target_session_id")
        _validate_non_empty_string(self.finding, "finding")
        _validate_evidence(self.evidence)
        if self.recommended_decision is not None and not isinstance(
            self.recommended_decision, Decision
        ):
            raise ValueError("recommended_decision must be a Decision or None")

    def to_json(self) -> str:
        """Serialize this report using the project protocol's wire field names."""

        payload: dict[str, Any] = {
            "auditId": self.audit_id,
            "targetSessionId": self.target_session_id,
            "finding": self.finding,
            "evidence": self.evidence,
        }
        if self.recommended_decision is not None:
            payload["recommendedDecision"] = self.recommended_decision.value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, document: str) -> AuditReport:
        """Parse and validate one AuditReport JSON object."""

        payload = _parse_json_object(document, "AuditReport")
        recommended = payload.get("recommendedDecision")
        return cls(
            audit_id=_required_string(payload, "auditId"),
            target_session_id=_required_string(payload, "targetSessionId"),
            finding=_required_string(payload, "finding"),
            evidence=_required_evidence(payload),
            recommended_decision=(
                None
                if recommended is None
                else _parse_decision(recommended, "recommendedDecision")
            ),
        )


@dataclass(frozen=True)
class PlannerDecision:
    """Minimal project Planner response to an AuditReport."""

    audit_id: str
    decision: Decision
    target_session_id: str
    instruction: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.audit_id, "audit_id")
        if not isinstance(self.decision, Decision):
            raise ValueError("decision must be a Decision")
        _validate_non_empty_string(self.target_session_id, "target_session_id")
        _validate_optional_string(self.instruction, "instruction")
        _validate_optional_string(self.reason, "reason")

    def to_json(self) -> str:
        """Serialize this decision using the project protocol's wire field names."""

        payload: dict[str, Any] = {
            "auditId": self.audit_id,
            "decision": self.decision.value,
            "targetSessionId": self.target_session_id,
        }
        if self.instruction is not None:
            payload["instruction"] = self.instruction
        if self.reason is not None:
            payload["reason"] = self.reason
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, document: str) -> PlannerDecision:
        """Parse and validate one PlannerDecision JSON object."""

        payload = _parse_json_object(document, "PlannerDecision")
        if "decision" not in payload:
            raise ValueError("PlannerDecision is missing required field 'decision'")
        return cls(
            audit_id=_required_string(payload, "auditId"),
            decision=_parse_decision(payload["decision"], "decision"),
            target_session_id=_required_string(payload, "targetSessionId"),
            instruction=_optional_string(payload, "instruction"),
            reason=_optional_string(payload, "reason"),
        )


def _parse_json_object(document: str, description: str) -> dict[str, Any]:
    if not isinstance(document, str):
        raise ValueError(f"{description} JSON must be a string")
    try:
        payload = json.loads(document)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{description} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{description} JSON must be an object")
    return payload


def _required_string(payload: dict[str, Any], key: str) -> str:
    if key not in payload:
        raise ValueError(f"missing required field {key!r}")
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {key!r} must be a non-empty string")
    return value


def _required_non_blank_string(payload: dict[str, Any], key: str) -> str:
    if key not in payload:
        raise ValueError(f"missing required field {key!r}")
    value = payload[key]
    _validate_non_blank_string(value, f"field {key!r}")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {key!r} must be a non-empty string or null")
    return value


def _required_evidence(payload: dict[str, Any]) -> list[str]:
    return _required_string_list(payload, "evidence")


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    if key not in payload:
        raise ValueError(f"missing required field {key!r}")
    value = payload[key]
    _validate_string_list(value, key)
    return value


def _required_audit_request_string_list(
    payload: dict[str, Any], key: str, *, require_item: bool = False
) -> list[str]:
    if key not in payload:
        raise ValueError(f"missing required field {key!r}")
    value = payload[key]
    _validate_audit_request_string_list(
        value, key, require_item=require_item
    )
    return value


def _validate_exact_fields(
    payload: dict[str, Any], expected: set[str], description: str
) -> None:
    missing = sorted(expected - payload.keys())
    if missing:
        raise ValueError(
            f"{description} is missing required field {missing[0]!r}"
        )
    unexpected = sorted(payload.keys() - expected)
    if unexpected:
        raise ValueError(
            f"{description} contains unexpected field {unexpected[0]!r}"
        )


def _parse_decision(value: object, key: str) -> Decision:
    if not isinstance(value, str):
        raise ValueError(f"field {key!r} must be a string")
    try:
        return Decision(value)
    except ValueError as exc:
        allowed = ", ".join(decision.value for decision in Decision)
        raise ValueError(f"field {key!r} must be one of: {allowed}") from exc


def _validate_non_empty_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _validate_non_blank_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _validate_optional_string(value: object, name: str) -> None:
    if value is not None:
        _validate_non_empty_string(value, name)


def _validate_evidence(value: object) -> None:
    _validate_string_list(value, "evidence")


def _validate_string_list(value: object, name: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty strings")


def _validate_audit_request_string_list(
    value: object, name: str, *, require_item: bool = False
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of non-blank strings")
    if require_item and not value:
        raise ValueError(f"{name} must contain at least one item")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{name} must be a list of non-blank strings")
