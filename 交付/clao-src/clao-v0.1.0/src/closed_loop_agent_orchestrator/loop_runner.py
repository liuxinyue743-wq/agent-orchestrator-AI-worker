"""Run synchronous audited and Planner -> Worker feedback loops."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .ao_client import AOClient
from .integration_gate import IntegrationGateResult, run_integration_gate
from .observer import (
    Observation,
    ObserverError,
    ObserverResult,
    ObserverTrigger,
    capture_observation,
    evaluate_observation,
)
from .protocol import AuditReport, AuditRequest, Decision, PlannerDecision


DECISIONS = frozenset(decision.value for decision in Decision)
_TURN_STATES = frozenset(
    {"queued", "running", "completed", "recovered", "interrupted", "failed"}
)
_UNSUCCESSFUL_TERMINAL_STATES = frozenset(
    {"recovered", "interrupted", "failed"}
)
_SAFE_ACTIVITY_STATES = frozenset({"idle", "waiting_input"})
_AUDITABLE_WORKER_STATES = frozenset(
    {*_SAFE_ACTIVITY_STATES, "active"}
)


class LoopRunError(RuntimeError):
    """Base error for deterministic loop failures."""

    code = "runtime_error"


class LoopProtocolError(LoopRunError):
    """AO or Planner returned an invalid protocol payload."""

    code = "protocol_error"


class LoopTimeoutError(LoopRunError):
    """A tracked AO turn did not finish before its deadline."""

    code = "timeout"


class UnsafeSessionError(LoopRunError):
    """A target session is not safe for automatic message delivery."""

    code = "unsafe_session"


class TurnFailedError(LoopRunError):
    """A tracked AO turn reached a non-success terminal state."""

    code = "turn_failed"


@dataclass(frozen=True)
class RunResult:
    """Structured result of one loop execution."""

    decision: PlannerDecision
    planner_turn_id: str
    planner_client_message_id: str
    worker_turn_id: str | None = None
    worker_client_message_id: str | None = None
    worker_response: str | None = None
    audit_report: AuditReport | None = None
    auditor_turn_id: str | None = None
    auditor_client_message_id: str | None = None
    worker_delivery_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "auditId": self.decision.audit_id,
            "decision": self.decision.decision.value,
            "targetSessionId": self.decision.target_session_id,
            "plannerTurnId": self.planner_turn_id,
            "plannerClientMessageId": self.planner_client_message_id,
            "workerTurnId": self.worker_turn_id,
            "workerClientMessageId": self.worker_client_message_id,
            "workerResponse": self.worker_response,
        }
        if self.decision.instruction is not None:
            payload["instruction"] = self.decision.instruction
        if self.decision.reason is not None:
            payload["reason"] = self.decision.reason
        if self.audit_report is not None:
            report: dict[str, Any] = {
                "auditId": self.audit_report.audit_id,
                "targetSessionId": self.audit_report.target_session_id,
                "finding": self.audit_report.finding,
                "evidence": list(self.audit_report.evidence),
            }
            if self.audit_report.recommended_decision is not None:
                report["recommendedDecision"] = (
                    self.audit_report.recommended_decision.value
                )
            payload["auditReport"] = report
            payload["auditorTurnId"] = self.auditor_turn_id
            payload["auditorClientMessageId"] = self.auditor_client_message_id
        if self.worker_delivery_error is not None:
            payload["workerDeliveryError"] = self.worker_delivery_error
        return payload


@dataclass(frozen=True)
class GatedRunResult:
    """Structured result of one Integration Gate and optional audited loop."""

    gate_result: IntegrationGateResult
    gate_audit_id: str | None = None
    audited_result: RunResult | None = None
    feedback_skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate_result.to_dict(),
            "gateAuditId": self.gate_audit_id,
            "auditedResult": (
                None
                if self.audited_result is None
                else self.audited_result.as_dict()
            ),
            "feedbackSkippedReason": self.feedback_skipped_reason,
        }


@dataclass(frozen=True)
class ObservedLoopResult:
    """Structured result of one bounded Observer-driven audited loop."""

    root_audit_id: str
    termination: Decision
    reason: str
    rounds: tuple[dict[str, Any], ...]
    audit_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "auditId": self.root_audit_id,
            "termination": self.termination.value,
            "reason": self.reason,
            "rounds": list(self.rounds),
            "auditCount": self.audit_count,
        }


def run_once(
    client: AOClient,
    *,
    planner_session_id: str,
    worker_session_id: str,
    finding: str,
    audit_id: str | None = None,
    evidence: Sequence[str] = (),
    recommended_decision: str | None = None,
    poll_interval: float = 2.0,
    timeout: float = 90.0,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Run one safe, tracked feedback loop against two existing Chat sessions."""

    planner_session_id = _nonempty_string(planner_session_id, "planner_session_id")
    worker_session_id = _nonempty_string(worker_session_id, "worker_session_id")
    finding = _nonempty_string(finding, "finding")
    audit_id = (
        str(uuid.uuid4())
        if audit_id is None
        else _nonempty_string(audit_id, "audit_id")
    )
    _validate_polling(poll_interval, timeout)
    normalized_evidence = tuple(
        _nonempty_string(item, "evidence item") for item in evidence
    )
    recommended: Decision | None = None
    if recommended_decision is not None:
        recommended_decision = _nonempty_string(
            recommended_decision, "recommended_decision"
        )
        try:
            recommended = Decision(recommended_decision)
        except ValueError as exc:
            raise ValueError(
                "recommended_decision must be PASS, LOCAL_FIX, REPLAN, or HUMAN"
            ) from exc

    report = AuditReport(
        audit_id=audit_id,
        target_session_id=worker_session_id,
        finding=finding,
        evidence=list(normalized_evidence),
        recommended_decision=recommended,
    )
    return _run_planner_worker(
        client,
        planner_session_id=planner_session_id,
        worker_session_id=worker_session_id,
        report=report,
        poll_interval=poll_interval,
        timeout=timeout,
        clock=_clock,
        sleep=_sleep,
    )


def run_audited_once(
    client: AOClient,
    *,
    auditor_session_id: str,
    planner_session_id: str,
    worker_session_id: str,
    task_goal: str,
    acceptance_criteria: Sequence[str],
    constraints: Sequence[str] = (),
    evidence: Sequence[str] = (),
    audit_id: str | None = None,
    poll_interval: float = 2.0,
    timeout: float = 90.0,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
    _allow_active_worker: bool = False,
    _overall_deadline: float | None = None,
) -> RunResult:
    """Run a read-only Auditor before the existing Planner -> Worker loop."""

    auditor_session_id = _nonempty_string(
        auditor_session_id, "auditor_session_id"
    )
    planner_session_id = _nonempty_string(planner_session_id, "planner_session_id")
    worker_session_id = _nonempty_string(worker_session_id, "worker_session_id")
    if len({auditor_session_id, planner_session_id, worker_session_id}) != 3:
        raise UnsafeSessionError(
            "Auditor, Planner, and Worker session ids must be different"
        )
    task_goal = _nonempty_string(task_goal, "task_goal")
    audit_id = (
        str(uuid.uuid4())
        if audit_id is None
        else _nonempty_string(audit_id, "audit_id")
    )
    _validate_polling(poll_interval, timeout)
    if isinstance(acceptance_criteria, (str, bytes)):
        raise ValueError(
            "acceptance_criteria must be a sequence of strings, not str or bytes"
        )
    if isinstance(constraints, (str, bytes)):
        raise ValueError(
            "constraints must be a sequence of strings, not str or bytes"
        )
    if isinstance(evidence, (str, bytes)):
        raise ValueError(
            "evidence must be a sequence of strings, not str or bytes"
        )
    normalized_acceptance_criteria = tuple(
        _nonempty_string(item, "acceptance_criteria item")
        for item in acceptance_criteria
    )
    if not normalized_acceptance_criteria:
        raise ValueError("acceptance_criteria must contain at least one item")
    normalized_constraints = tuple(
        _nonempty_string(item, "constraints item") for item in constraints
    )
    normalized_evidence = tuple(
        _nonempty_string(item, "evidence item") for item in evidence
    )

    auditor_project_id = _require_safe_session(
        client.get_session(auditor_session_id),
        session_id=auditor_session_id,
        expected_kind="worker",
    )
    planner_project_id = _require_safe_session(
        client.get_session(planner_session_id),
        session_id=planner_session_id,
        expected_kind="orchestrator",
    )
    worker_project_id = _require_safe_session(
        client.get_session(worker_session_id),
        session_id=worker_session_id,
        expected_kind="worker",
        allowed_activity_states=(
            _AUDITABLE_WORKER_STATES
            if _allow_active_worker
            else _SAFE_ACTIVITY_STATES
        ),
    )
    if len({auditor_project_id, planner_project_id, worker_project_id}) != 1:
        raise UnsafeSessionError(
            "Auditor, Planner, and Worker sessions must belong to the same AO Project"
        )

    _require_clean_auditor_workspace(
        client.get_workspace_summary(auditor_session_id),
        session_id=auditor_session_id,
        phase="before",
    )
    request = AuditRequest(
        audit_id=audit_id,
        target_session_id=worker_session_id,
        task_goal=task_goal,
        acceptance_criteria=list(normalized_acceptance_criteria),
        constraints=list(normalized_constraints),
        evidence=list(normalized_evidence),
    )
    auditor_client_message_id = _client_message_id(audit_id, "auditor")
    try:
        auditor_turn_id = _send_message(
            client,
            auditor_session_id,
            _auditor_prompt(request),
            auditor_client_message_id,
        )
        auditor_text = _wait_for_assistant_text(
            client,
            auditor_session_id,
            auditor_turn_id,
            role="Auditor",
            poll_interval=poll_interval,
            timeout=timeout,
            clock=_clock,
            sleep=_sleep,
            overall_deadline=_overall_deadline,
        )
    finally:
        _require_clean_auditor_workspace(
            client.get_workspace_summary(auditor_session_id),
            session_id=auditor_session_id,
            phase="after",
        )

    report = _parse_audit_report(auditor_text)
    if report.audit_id != request.audit_id:
        raise LoopProtocolError("AuditReport auditId does not match AuditRequest")
    if report.target_session_id != request.target_session_id:
        raise LoopProtocolError(
            "AuditReport targetSessionId does not match AuditRequest"
        )
    if report.recommended_decision is None:
        raise LoopProtocolError("AuditReport requires recommendedDecision")

    result = _run_planner_worker(
        client,
        planner_session_id=planner_session_id,
        worker_session_id=worker_session_id,
        report=report,
        expected_project_id=auditor_project_id,
        poll_interval=poll_interval,
        timeout=timeout,
        clock=_clock,
        sleep=_sleep,
        allow_active_worker=_allow_active_worker,
        overall_deadline=_overall_deadline,
    )
    return replace(
        result,
        audit_report=report,
        auditor_turn_id=auditor_turn_id,
        auditor_client_message_id=auditor_client_message_id,
    )


def run_gated_once(
    client_factory: Callable[[], AOClient],
    *,
    auditor_session_id: str,
    planner_session_id: str,
    worker_session_id: str,
    task_goal: str,
    acceptance_criteria: Sequence[str],
    gate_repo: str,
    gate_commands: Sequence[Sequence[str]],
    constraints: Sequence[str] = (),
    evidence: Sequence[str] = (),
    audit_id: str,
    gate_timeout: float = 300.0,
    gate_output_limit: int = 20_000,
    poll_interval: float = 2.0,
    timeout: float = 90.0,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
    _gate_runner: Callable[..., IntegrationGateResult] = run_integration_gate,
    _audited_runner: Callable[..., RunResult] = run_audited_once,
) -> GatedRunResult:
    """Run the deterministic Gate, auditing only an executed-command failure."""

    gate_result = _gate_runner(
        gate_repo,
        gate_commands,
        timeout_seconds=gate_timeout,
        output_limit_chars=gate_output_limit,
    )
    if gate_result.passed:
        return GatedRunResult(
            gate_result=gate_result,
            feedback_skipped_reason=(
                "Integration Gate passed; Agent feedback was not required"
            ),
        )
    if not gate_result.steps or gate_result.commit_sha is None:
        return GatedRunResult(
            gate_result=gate_result,
            feedback_skipped_reason=(
                "Integration Gate precondition failed before any configured "
                "command executed"
            ),
        )

    gate_evidence = gate_result.to_evidence()
    gate_audit_id = _gate_audit_id(
        audit_id,
        gate_result.commit_sha,
        gate_evidence,
    )
    with client_factory() as client:
        audited_result = _audited_runner(
            client,
            auditor_session_id=auditor_session_id,
            planner_session_id=planner_session_id,
            worker_session_id=worker_session_id,
            task_goal=task_goal,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            evidence=(*evidence, gate_evidence),
            audit_id=gate_audit_id,
            poll_interval=poll_interval,
            timeout=timeout,
            _clock=_clock,
            _sleep=_sleep,
        )
    return GatedRunResult(
        gate_result=gate_result,
        gate_audit_id=gate_audit_id,
        audited_result=audited_result,
    )


def run_observed_loop(
    client: AOClient,
    *,
    auditor_session_id: str,
    planner_session_id: str,
    worker_session_id: str,
    task_goal: str,
    acceptance_criteria: Sequence[str],
    constraints: Sequence[str] = (),
    evidence: Sequence[str] = (),
    audit_id: str,
    observe_interval: float = 2.0,
    stall_threshold: float = 300.0,
    failure_threshold: int = 2,
    max_audits: int = 3,
    overall_timeout: float = 600.0,
    poll_interval: float = 2.0,
    timeout: float = 90.0,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
    _capture: Callable[[AOClient, str], Observation] = capture_observation,
    _audited_runner: Callable[..., RunResult] = run_audited_once,
) -> ObservedLoopResult:
    """Run a bounded foreground Observer -> audited-loop cycle."""

    auditor_session_id = _nonempty_string(
        auditor_session_id, "auditor_session_id"
    )
    planner_session_id = _nonempty_string(
        planner_session_id, "planner_session_id"
    )
    worker_session_id = _nonempty_string(worker_session_id, "worker_session_id")
    if len({auditor_session_id, planner_session_id, worker_session_id}) != 3:
        raise UnsafeSessionError(
            "Auditor, Planner, and Worker session ids must be different"
        )
    task_goal = _nonempty_string(task_goal, "task_goal")
    root_audit_id = _nonempty_string(audit_id, "audit_id")
    normalized_acceptance_criteria = _normalize_string_sequence(
        acceptance_criteria,
        "acceptance_criteria",
        require_item=True,
    )
    normalized_constraints = _normalize_string_sequence(
        constraints, "constraints"
    )
    normalized_evidence = _normalize_string_sequence(evidence, "evidence")
    _validate_polling(poll_interval, timeout)
    _validate_observed_limits(
        observe_interval=observe_interval,
        stall_threshold=stall_threshold,
        failure_threshold=failure_threshold,
        max_audits=max_audits,
        overall_timeout=overall_timeout,
    )

    started_at = _clock()
    overall_deadline = started_at + overall_timeout
    rounds: list[dict[str, Any]] = []
    audit_count = 0
    last_worker_response: str | None = None

    try:
        previous = _capture(client, worker_session_id)
    except ObserverError as exc:
        return _observed_human_result(
            root_audit_id,
            f"initial Worker observation failed: {exc}",
            rounds,
            audit_count,
        )
    rejection = _observation_rejection(previous)
    if rejection is not None:
        return _observed_human_result(
            root_audit_id, rejection, rounds, audit_count
        )

    no_progress_since = _clock()
    pending_observation: Observation | None = None
    while True:
        if pending_observation is None:
            remaining = overall_deadline - _clock()
            if remaining <= 0:
                return _observed_human_result(
                    root_audit_id,
                    "overall observation timeout reached",
                    rounds,
                    audit_count,
                )
            if remaining <= observe_interval:
                _sleep(remaining)
                return _observed_human_result(
                    root_audit_id,
                    "overall observation timeout reached",
                    rounds,
                    audit_count,
                )
            _sleep(observe_interval)
            try:
                current = _capture(client, worker_session_id)
            except ObserverError as exc:
                return _observed_human_result(
                    root_audit_id,
                    f"Worker observation failed: {exc}",
                    rounds,
                    audit_count,
                )
        else:
            current = pending_observation
            pending_observation = None

        captured_at = _clock()
        if captured_at >= overall_deadline:
            return _observed_human_result(
                root_audit_id,
                "overall observation timeout reached",
                rounds,
                audit_count,
            )
        rejection = _observation_rejection(current)
        if rejection is not None:
            return _observed_human_result(
                root_audit_id, rejection, rounds, audit_count
            )
        if (
            current.progress_signature != previous.progress_signature
            or current.activity_state != previous.activity_state
        ):
            no_progress_since = captured_at
        observer_result = evaluate_observation(
            previous,
            current,
            captured_at - no_progress_since,
            repeated_failure_threshold=failure_threshold,
            stall_threshold_seconds=stall_threshold,
        )
        previous = current
        if observer_result is None:
            continue
        if audit_count >= max_audits:
            return _observed_human_result(
                root_audit_id,
                f"maximum audit count {max_audits} reached",
                rounds,
                audit_count,
            )

        cycle_audit_id = _cycle_audit_id(
            root_audit_id, current, observer_result.trigger
        )
        round_payload = _empty_observed_round(
            cycle_audit_id, observer_result
        )
        rounds.append(round_payload)
        audit_count += 1
        cycle_evidence = (
            *normalized_evidence,
            f"Observer trigger: {observer_result.trigger.value}",
            *(
                f"Observer evidence: {item}"
                for item in observer_result.evidence
            ),
        )
        if last_worker_response is not None:
            cycle_evidence = (
                *cycle_evidence,
                "Previous LOCAL_FIX worker response: " + last_worker_response,
            )

        try:
            audited_result = _audited_runner(
                client,
                auditor_session_id=auditor_session_id,
                planner_session_id=planner_session_id,
                worker_session_id=worker_session_id,
                task_goal=task_goal,
                acceptance_criteria=normalized_acceptance_criteria,
                constraints=normalized_constraints,
                evidence=cycle_evidence,
                audit_id=cycle_audit_id,
                poll_interval=poll_interval,
                timeout=timeout,
                _clock=_clock,
                _sleep=_sleep,
                _allow_active_worker=True,
                _overall_deadline=overall_deadline,
            )
        except (LoopTimeoutError, UnsafeSessionError) as exc:
            round_payload["error"] = str(exc)
            return _observed_human_result(
                root_audit_id, str(exc), rounds, audit_count
            )

        _complete_observed_round(round_payload, audited_result)
        if _clock() >= overall_deadline:
            return _observed_human_result(
                root_audit_id,
                "overall observation timeout reached",
                rounds,
                audit_count,
            )
        if audited_result.worker_delivery_error is not None:
            return _observed_human_result(
                root_audit_id,
                audited_result.worker_delivery_error,
                rounds,
                audit_count,
            )

        decision = audited_result.decision.decision
        if decision is not Decision.LOCAL_FIX:
            return ObservedLoopResult(
                root_audit_id=root_audit_id,
                termination=decision,
                reason=(
                    audited_result.decision.reason
                    or f"Planner returned {decision.value}"
                ),
                rounds=tuple(rounds),
                audit_count=audit_count,
            )
        if audited_result.worker_response is None:
            return _observed_human_result(
                root_audit_id,
                "LOCAL_FIX completed without a Worker response",
                rounds,
                audit_count,
            )
        if audit_count >= max_audits:
            return _observed_human_result(
                root_audit_id,
                f"maximum audit count {max_audits} reached",
                rounds,
                audit_count,
            )

        last_worker_response = audited_result.worker_response
        try:
            pending_observation = _capture(client, worker_session_id)
        except ObserverError as exc:
            return _observed_human_result(
                root_audit_id,
                f"post-LOCAL_FIX Worker observation failed: {exc}",
                rounds,
                audit_count,
            )


def _observed_human_result(
    root_audit_id: str,
    reason: str,
    rounds: Sequence[dict[str, Any]],
    audit_count: int,
) -> ObservedLoopResult:
    return ObservedLoopResult(
        root_audit_id=root_audit_id,
        termination=Decision.HUMAN,
        reason=reason,
        rounds=tuple(rounds),
        audit_count=audit_count,
    )


def _observation_rejection(observation: Observation) -> str | None:
    if observation.activity_state in {"blocked", "exited", "terminated"}:
        return (
            f"Worker activity {observation.activity_state!r} is not safe for "
            "automatic observation or delivery"
        )
    return None


def _normalize_string_sequence(
    value: Sequence[str],
    name: str,
    *,
    require_item: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of strings, not str or bytes")
    normalized = tuple(
        _nonempty_string(item, f"{name} item") for item in value
    )
    if require_item and not normalized:
        raise ValueError(f"{name} must contain at least one item")
    return normalized


def _validate_observed_limits(
    *,
    observe_interval: float,
    stall_threshold: float,
    failure_threshold: int,
    max_audits: int,
    overall_timeout: float,
) -> None:
    if (
        isinstance(observe_interval, bool)
        or not isinstance(observe_interval, (int, float))
        or not math.isfinite(observe_interval)
        or observe_interval <= 0
    ):
        raise ValueError("observe_interval must be greater than zero")
    if (
        isinstance(stall_threshold, bool)
        or not isinstance(stall_threshold, (int, float))
        or not math.isfinite(stall_threshold)
        or stall_threshold < 0
    ):
        raise ValueError("stall_threshold must be non-negative")
    if (
        not isinstance(failure_threshold, int)
        or isinstance(failure_threshold, bool)
        or failure_threshold < 1
    ):
        raise ValueError("failure_threshold must be an integer >= 1")
    if (
        not isinstance(max_audits, int)
        or isinstance(max_audits, bool)
        or max_audits < 1
    ):
        raise ValueError("max_audits must be an integer >= 1")
    if (
        isinstance(overall_timeout, bool)
        or not isinstance(overall_timeout, (int, float))
        or not math.isfinite(overall_timeout)
        or overall_timeout <= 0
    ):
        raise ValueError("overall_timeout must be greater than zero")


def _cycle_audit_id(
    root_audit_id: str,
    observation: Observation,
    trigger: ObserverTrigger,
) -> str:
    signature = (
        observation.activity_state,
        tuple((item.id, item.state) for item in observation.turns),
        tuple(
            (item.id, item.revision, item.streaming)
            for item in observation.messages
        ),
        tuple(
            (item.id, item.revision, item.activity_kind, item.status)
            for item in observation.activities
        ),
        tuple((item.path, item.status) for item in observation.workspace_files),
        observation.commit_shas,
        tuple(
            (item.source_id, item.fingerprint)
            for item in observation.failures
        ),
    )
    seed = json.dumps(
        [
            root_audit_id,
            observation.worker_session_id,
            trigger.value,
            signature,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    return f"{root_audit_id}:{identifier}"


def _gate_audit_id(
    root_audit_id: str,
    commit_sha: str,
    gate_evidence: str,
) -> str:
    root_audit_id = _nonempty_string(root_audit_id, "audit_id")
    commit_sha = _nonempty_string(commit_sha, "Gate commit_sha")
    gate_evidence = _nonempty_string(gate_evidence, "Gate evidence")
    seed = json.dumps(
        [root_audit_id, commit_sha, gate_evidence],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    return f"{root_audit_id}:{identifier}"


def _empty_observed_round(
    cycle_audit_id: str, observer_result: ObserverResult
) -> dict[str, Any]:
    return {
        "cycleAuditId": cycle_audit_id,
        "observer": {
            "trigger": observer_result.trigger.value,
            "evidence": list(observer_result.evidence),
        },
        "auditReport": None,
        "plannerDecision": None,
        "turns": {
            "auditor": {"turnId": None, "clientMessageId": None},
            "planner": {"turnId": None, "clientMessageId": None},
            "worker": {"turnId": None, "clientMessageId": None},
        },
        "workerResponse": None,
    }


def _complete_observed_round(
    payload: dict[str, Any], result: RunResult
) -> None:
    serialized = result.as_dict()
    payload["auditReport"] = serialized.get("auditReport")
    decision: dict[str, Any] = {
        "auditId": result.decision.audit_id,
        "decision": result.decision.decision.value,
        "targetSessionId": result.decision.target_session_id,
    }
    if result.decision.instruction is not None:
        decision["instruction"] = result.decision.instruction
    if result.decision.reason is not None:
        decision["reason"] = result.decision.reason
    payload["plannerDecision"] = decision
    payload["turns"] = {
        "auditor": {
            "turnId": result.auditor_turn_id,
            "clientMessageId": result.auditor_client_message_id,
        },
        "planner": {
            "turnId": result.planner_turn_id,
            "clientMessageId": result.planner_client_message_id,
        },
        "worker": {
            "turnId": result.worker_turn_id,
            "clientMessageId": result.worker_client_message_id,
        },
    }
    payload["workerResponse"] = result.worker_response
    if result.worker_delivery_error is not None:
        payload["error"] = result.worker_delivery_error


def _run_planner_worker(
    client: AOClient,
    *,
    planner_session_id: str,
    worker_session_id: str,
    report: AuditReport,
    poll_interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    expected_project_id: str | None = None,
    allow_active_worker: bool = False,
    overall_deadline: float | None = None,
) -> RunResult:
    planner_project_id = _require_safe_session(
        client.get_session(planner_session_id),
        session_id=planner_session_id,
        expected_kind="orchestrator",
    )
    worker_project_id = _require_safe_session(
        client.get_session(worker_session_id),
        session_id=worker_session_id,
        expected_kind="worker",
        allowed_activity_states=(
            _AUDITABLE_WORKER_STATES
            if allow_active_worker
            else _SAFE_ACTIVITY_STATES
        ),
    )
    if worker_project_id != planner_project_id:
        raise UnsafeSessionError(
            "Planner and Worker sessions must belong to the same AO Project"
        )
    if expected_project_id is not None and planner_project_id != expected_project_id:
        raise UnsafeSessionError(
            "Planner and Worker sessions no longer belong to the initially "
            "validated AO Project"
        )

    planner_client_message_id = _client_message_id(report.audit_id, "planner")
    planner_turn_id = _send_message(
        client,
        planner_session_id,
        _planner_prompt(report),
        planner_client_message_id,
    )
    decision = _wait_for_planner_decision(
        client,
        planner_session_id,
        planner_turn_id,
        poll_interval=poll_interval,
        timeout=timeout,
        clock=clock,
        sleep=sleep,
        overall_deadline=overall_deadline,
    )
    if decision.audit_id != report.audit_id:
        raise LoopProtocolError("PlannerDecision auditId does not match AuditReport")
    if decision.target_session_id != worker_session_id:
        raise LoopProtocolError(
            "PlannerDecision targetSessionId does not match the requested Worker"
        )

    if allow_active_worker:
        try:
            current_worker_project_id = _require_safe_session(
                client.get_session(worker_session_id),
                session_id=worker_session_id,
                expected_kind="worker",
                allowed_activity_states=_AUDITABLE_WORKER_STATES,
            )
        except (LoopProtocolError, UnsafeSessionError) as exc:
            return RunResult(
                decision=decision,
                planner_turn_id=planner_turn_id,
                planner_client_message_id=planner_client_message_id,
                worker_delivery_error=str(exc),
            )
        if current_worker_project_id != planner_project_id:
            return RunResult(
                decision=decision,
                planner_turn_id=planner_turn_id,
                planner_client_message_id=planner_client_message_id,
                worker_delivery_error=(
                    "Worker session no longer belongs to the initially "
                    "validated AO Project"
                ),
            )

    if decision.decision is not Decision.LOCAL_FIX:
        return RunResult(
            decision=decision,
            planner_turn_id=planner_turn_id,
            planner_client_message_id=planner_client_message_id,
        )

    if decision.instruction is None or not decision.instruction.strip():
        raise LoopProtocolError("LOCAL_FIX PlannerDecision requires instruction")

    if allow_active_worker:
        delivery_error = _wait_for_safe_worker_delivery(
            client,
            session_id=worker_session_id,
            expected_project_id=planner_project_id,
            poll_interval=poll_interval,
            timeout=timeout,
            clock=clock,
            sleep=sleep,
            overall_deadline=overall_deadline,
        )
        if delivery_error is not None:
            return RunResult(
                decision=decision,
                planner_turn_id=planner_turn_id,
                planner_client_message_id=planner_client_message_id,
                worker_delivery_error=delivery_error,
            )
    else:
        current_worker_project_id = _require_safe_session(
            client.get_session(worker_session_id),
            session_id=worker_session_id,
            expected_kind="worker",
        )
        if current_worker_project_id != planner_project_id:
            raise UnsafeSessionError(
                "Worker session no longer belongs to the initially validated "
                "AO Project"
            )
    worker_client_message_id = _client_message_id(report.audit_id, "worker")
    worker_turn_id = _send_message(
        client,
        worker_session_id,
        decision.instruction,
        worker_client_message_id,
    )
    worker_response = _wait_for_worker_response(
        client,
        worker_session_id,
        worker_turn_id,
        poll_interval=poll_interval,
        timeout=timeout,
        clock=clock,
        sleep=sleep,
        overall_deadline=overall_deadline,
    )
    return RunResult(
        decision=decision,
        planner_turn_id=planner_turn_id,
        planner_client_message_id=planner_client_message_id,
        worker_turn_id=worker_turn_id,
        worker_client_message_id=worker_client_message_id,
        worker_response=worker_response,
    )


def _auditor_prompt(request: AuditRequest) -> str:
    return (
        f"AuditRequest: {request.to_json()}\n"
        "Act as a read-only Auditor. Return exactly one line containing only an "
        "AuditReport JSON object. Required fields: auditId, targetSessionId, "
        "finding, evidence, recommendedDecision. recommendedDecision must be one "
        "of PASS, LOCAL_FIX, REPLAN, HUMAN. Do not modify files, create commits, "
        "or create, stop, restore, delegate, schedule, or message any Worker."
    )


def _planner_prompt(report: AuditReport) -> str:
    return (
        f"AuditReport: {report.to_json()}\n"
        "Return exactly one line containing only a PlannerDecision JSON object. "
        "Required fields: auditId, decision, targetSessionId. decision must be one "
        "of PASS, LOCAL_FIX, REPLAN, HUMAN. LOCAL_FIX must include a non-empty "
        "instruction; reason is optional. Do not create, stop, restore, or delegate "
        "a Worker."
    )


def _require_clean_auditor_workspace(
    summary: dict[str, Any], *, session_id: str, phase: str
) -> None:
    if summary.get("sessionId") != session_id:
        raise LoopProtocolError(
            "Auditor workspace sessionId does not match the request"
        )
    if summary.get("truncated") is not False:
        raise UnsafeSessionError(
            f"Auditor workspace summary is truncated {phase} execution"
        )
    files = summary.get("files")
    commits = summary.get("commits")
    if not isinstance(files, list) or not all(
        isinstance(item, dict) for item in files
    ):
        raise LoopProtocolError("Auditor workspace files must be objects")
    if not isinstance(commits, list) or not all(
        isinstance(item, dict) for item in commits
    ):
        raise LoopProtocolError("Auditor workspace commits must be objects")
    changed_files = [
        item for item in files if item.get("status") != "unmodified"
    ]
    if changed_files or commits:
        raise UnsafeSessionError(
            "Auditor workspace must have no changed files or additional commits "
            f"{phase} execution"
        )


def _require_safe_session(
    session: dict[str, Any],
    *,
    session_id: str,
    expected_kind: str,
    allowed_activity_states: frozenset[str] = _SAFE_ACTIVITY_STATES,
) -> str:
    if session.get("id") != session_id:
        raise LoopProtocolError("AO session response id does not match the request")
    if session.get("kind") != expected_kind:
        raise UnsafeSessionError(
            f"session {session_id!r} must have kind {expected_kind!r}"
        )
    if session.get("mode") != "chat":
        raise UnsafeSessionError(f"session {session_id!r} must use Chat mode")
    if session.get("isTerminated") is not False:
        raise UnsafeSessionError(f"session {session_id!r} is terminated or unknown")

    activity = session.get("activity")
    state = activity.get("state") if isinstance(activity, dict) else None
    status = session.get("status")
    if state in {"blocked", "exited"} or status in {"exited", "terminated"}:
        raise UnsafeSessionError(
            f"session {session_id!r} is not safe for automatic input"
        )
    if state not in allowed_activity_states:
        raise UnsafeSessionError(
            f"session {session_id!r} is not in an allowed activity state"
        )
    project_id = session.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        raise UnsafeSessionError(
            f"session {session_id!r} must have a non-empty projectId"
        )
    return project_id


def _wait_for_safe_worker_delivery(
    client: AOClient,
    *,
    session_id: str,
    expected_project_id: str,
    poll_interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    overall_deadline: float | None,
) -> str | None:
    deadline = clock() + timeout
    if overall_deadline is not None:
        deadline = min(deadline, overall_deadline)
    while True:
        session = client.get_session(session_id)
        try:
            project_id = _require_safe_session(
                session,
                session_id=session_id,
                expected_kind="worker",
            )
        except UnsafeSessionError as exc:
            try:
                active_project_id = _require_safe_session(
                    session,
                    session_id=session_id,
                    expected_kind="worker",
                    allowed_activity_states=_AUDITABLE_WORKER_STATES,
                )
            except (LoopProtocolError, UnsafeSessionError):
                return str(exc)
            if active_project_id != expected_project_id:
                return (
                    "Worker session no longer belongs to the initially "
                    "validated AO Project"
                )
        except LoopProtocolError as exc:
            return str(exc)
        else:
            if project_id != expected_project_id:
                return (
                    "Worker session no longer belongs to the initially "
                    "validated AO Project"
                )
            return None

        remaining = deadline - clock()
        if remaining <= 0:
            return "timed out waiting for Worker to become safe for LOCAL_FIX"
        sleep(min(poll_interval, remaining))


def _send_message(
    client: AOClient, session_id: str, text: str, client_message_id: str
) -> str:
    payload = client.send_conversation_message(
        session_id,
        text,
        client_message_id,
    )
    duplicate = payload.get("duplicate")
    if not isinstance(duplicate, bool):
        raise LoopProtocolError("AO message response duplicate must be a boolean")
    if duplicate:
        return _recover_duplicate_turn(
            client,
            session_id=session_id,
            text=text,
        )
    turn_id = payload.get("turnId")
    state = payload.get("state")
    if not isinstance(turn_id, str) or not turn_id:
        raise LoopProtocolError("AO message response is missing turnId")
    if state not in _TURN_STATES:
        raise LoopProtocolError("AO message response has an invalid turn state")
    return turn_id


def _recover_duplicate_turn(
    client: AOClient,
    *,
    session_id: str,
    text: str,
) -> str:
    snapshot = client.get_conversation(session_id, limit=500)
    if snapshot.get("sessionId") != session_id:
        raise LoopProtocolError(
            "duplicate recovery conversation sessionId does not match the request"
        )
    messages = snapshot.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(item, dict) for item in messages
    ):
        raise LoopProtocolError(
            "duplicate recovery conversation messages must be objects"
        )
    matching_messages = [
        item
        for item in messages
        if item.get("role") == "user"
        and item.get("origin") == "human"
        and item.get("text") == text
    ]
    if not matching_messages:
        raise LoopProtocolError(
            "duplicate recovery could not find an exact original user message"
        )
    if len(matching_messages) > 1:
        raise LoopProtocolError(
            "duplicate recovery found multiple original user messages"
        )
    message = matching_messages[0]
    turn_id = message.get("turnId")
    if not isinstance(turn_id, str) or not turn_id:
        raise LoopProtocolError(
            "duplicate recovery original user message is missing turnId"
        )
    return turn_id


def _client_message_id(audit_id: str, phase: str) -> str:
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"clao:{audit_id}:{phase}")
    return f"clao-{phase}-{identifier}"


def _wait_for_planner_decision(
    client: AOClient,
    session_id: str,
    turn_id: str,
    *,
    poll_interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    overall_deadline: float | None = None,
) -> PlannerDecision:
    text = _wait_for_assistant_text(
        client,
        session_id,
        turn_id,
        role="Planner",
        poll_interval=poll_interval,
        timeout=timeout,
        clock=clock,
        sleep=sleep,
        overall_deadline=overall_deadline,
    )
    return _parse_planner_decision(text)


def _wait_for_worker_response(
    client: AOClient,
    session_id: str,
    turn_id: str,
    *,
    poll_interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    overall_deadline: float | None = None,
) -> str:
    return _wait_for_assistant_text(
        client,
        session_id,
        turn_id,
        role="Worker",
        poll_interval=poll_interval,
        timeout=timeout,
        clock=clock,
        sleep=sleep,
        overall_deadline=overall_deadline,
    )


def _wait_for_assistant_text(
    client: AOClient,
    session_id: str,
    turn_id: str,
    *,
    role: str,
    poll_interval: float,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    overall_deadline: float | None = None,
) -> str:
    deadline = clock() + timeout
    if overall_deadline is not None:
        deadline = min(deadline, overall_deadline)
    while True:
        snapshot = client.get_conversation(session_id, limit=500)
        if snapshot.get("sessionId") != session_id:
            raise LoopProtocolError(
                f"{role} conversation sessionId does not match the request"
            )
        turns = snapshot.get("turns")
        messages = snapshot.get("messages")
        if not isinstance(turns, list) or not all(
            isinstance(item, dict) for item in turns
        ):
            raise LoopProtocolError(f"{role} conversation turns must be objects")
        if not isinstance(messages, list) or not all(
            isinstance(item, dict) for item in messages
        ):
            raise LoopProtocolError(f"{role} conversation messages must be objects")

        matching_turns = [item for item in turns if item.get("id") == turn_id]
        if len(matching_turns) > 1:
            raise LoopProtocolError(f"{role} conversation contains duplicate turn ids")
        if matching_turns:
            turn_state = matching_turns[0].get("state")
            if turn_state not in _TURN_STATES:
                raise LoopProtocolError(f"{role} turn has an invalid state")
            if turn_state in _UNSUCCESSFUL_TERMINAL_STATES:
                detail = matching_turns[0].get("errorMessage")
                suffix = f": {detail}" if isinstance(detail, str) and detail else ""
                raise TurnFailedError(
                    f"{role} turn ended in state {turn_state!r}{suffix}"
                )
            if turn_state == "completed":
                assistant_messages = [
                    item
                    for item in messages
                    if item.get("turnId") == turn_id
                    and item.get("role") == "assistant"
                ]
                if len(assistant_messages) > 1:
                    raise LoopProtocolError(
                        f"{role} turn contains multiple assistant messages"
                    )
                if assistant_messages:
                    message = assistant_messages[0]
                    streaming = message.get("streaming")
                    if not isinstance(streaming, bool):
                        raise LoopProtocolError(
                            f"{role} assistant message streaming must be a boolean"
                        )
                    text = message.get("text")
                    if not isinstance(text, str):
                        raise LoopProtocolError(
                            f"{role} assistant message text must be a string"
                        )
                    if not streaming and text.strip():
                        return text

        remaining = deadline - clock()
        if remaining <= 0:
            raise LoopTimeoutError(
                f"timed out waiting for {role} turn {turn_id!r}"
            )
        sleep(min(poll_interval, remaining))


def _parse_planner_decision(text: str) -> PlannerDecision:
    stripped = text.strip()
    if "\n" in stripped or "\r" in stripped:
        raise LoopProtocolError("PlannerDecision must be exactly one line of JSON")
    try:
        decision = PlannerDecision.from_json(stripped)
    except ValueError as exc:
        raise LoopProtocolError(str(exc)) from exc
    if decision.decision is Decision.LOCAL_FIX and (
        decision.instruction is None or not decision.instruction.strip()
    ):
        raise LoopProtocolError("LOCAL_FIX PlannerDecision requires instruction")
    return decision


def _parse_audit_report(text: str) -> AuditReport:
    stripped = text.strip()
    if "\n" in stripped or "\r" in stripped:
        raise LoopProtocolError("AuditReport must be exactly one line of JSON")
    try:
        return AuditReport.from_json(stripped)
    except ValueError as exc:
        raise LoopProtocolError(str(exc)) from exc


def _validate_polling(poll_interval: float, timeout: float) -> None:
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be greater than zero")


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
