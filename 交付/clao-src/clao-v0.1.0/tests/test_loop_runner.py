from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from closed_loop_agent_orchestrator.ao_client import AOClient
from closed_loop_agent_orchestrator.loop_runner import (
    GatedRunResult,
    LoopProtocolError,
    LoopTimeoutError,
    ObservedLoopResult,
    RunResult,
    TurnFailedError,
    UnsafeSessionError,
    run_audited_once,
    run_gated_once,
    run_observed_loop,
    run_once,
)
from closed_loop_agent_orchestrator.integration_gate import (
    GateStepResult,
    IntegrationGateResult,
)
from closed_loop_agent_orchestrator.observer import (
    ActivityObservation,
    FailureOccurrence,
    MessageObservation,
    Observation,
    TurnObservation,
    WorkspaceFileObservation,
)
from closed_loop_agent_orchestrator.protocol import (
    AuditReport,
    Decision,
    PlannerDecision,
)


AUDITOR_ID = "auditor-1"
PLANNER_ID = "planner-1"
WORKER_ID = "worker-1"
PROJECT_ID = "project-1"
GATE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass(frozen=True)
class Step:
    state: str
    text: str | Callable[[dict[str, Any]], str] | None = None
    streaming: bool = False
    revision: int = 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def safe_session(session_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "projectId": PROJECT_ID,
        "kind": kind,
        "mode": "chat",
        "isTerminated": False,
        "activity": {"state": "idle"},
        "status": "idle",
    }


def decision_text(
    report: dict[str, Any],
    *,
    decision: str = "LOCAL_FIX",
    instruction: str | None = "fix only the failing assertion",
    audit_id: str | None = None,
    target_session_id: str = WORKER_ID,
) -> str:
    payload = {
        "auditId": audit_id or report["auditId"],
        "decision": decision,
        "targetSessionId": target_session_id,
        "reason": "evidence supports this decision",
    }
    if instruction is not None:
        payload["instruction"] = instruction
    return json.dumps(payload, separators=(",", ":"))


def audit_report_text(
    request: dict[str, Any],
    *,
    recommended_decision: str | None = "LOCAL_FIX",
    audit_id: str | None = None,
    target_session_id: str = WORKER_ID,
) -> str:
    payload: dict[str, Any] = {
        "auditId": audit_id or request["auditId"],
        "targetSessionId": target_session_id,
        "finding": "acceptance evidence is insufficient",
        "evidence": ["pytest: one failure"],
    }
    if recommended_decision is not None:
        payload["recommendedDecision"] = recommended_decision
    return json.dumps(payload, separators=(",", ":"))


def clean_workspace() -> dict[str, Any]:
    return {
        "sessionId": AUDITOR_ID,
        "files": [],
        "commits": [],
        "truncated": False,
    }


class FakeAO:
    def __init__(
        self,
        *,
        auditor_steps: list[Step] | None = None,
        planner_steps: list[Step] | None = None,
        worker_steps: list[Step] | None = None,
        workspace_steps: list[dict[str, Any]] | None = None,
        duplicate_modes: dict[str, str] | None = None,
    ) -> None:
        self.auditor_steps = auditor_steps or [
            Step("completed", lambda request: audit_report_text(request))
        ]
        self.planner_steps = planner_steps or [
            Step("completed", lambda report: decision_text(report))
        ]
        self.worker_steps = worker_steps or [Step("completed", "worker finished")]
        self.workspace_steps = workspace_steps or [clean_workspace()]
        self.auditor_session = safe_session(AUDITOR_ID, "worker")
        self.planner_session = safe_session(PLANNER_ID, "orchestrator")
        self.worker_sessions = [safe_session(WORKER_ID, "worker")]
        self.audit_request: dict[str, Any] | None = None
        self.report: dict[str, Any] | None = None
        self.requests: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.polls = {AUDITOR_ID: 0, PLANNER_ID: 0, WORKER_ID: 0}
        self.workspace_reads = 0
        self.worker_session_reads = 0
        self.duplicate_modes = duplicate_modes or {}
        self.user_messages = {AUDITOR_ID: [], PLANNER_ID: [], WORKER_ID: []}
        self.logical_turns = {AUDITOR_ID: 0, PLANNER_ID: 0, WORKER_ID: 0}
        self.accepted_client_message_ids = {
            AUDITOR_ID: set(),
            PLANNER_ID: set(),
            WORKER_ID: set(),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        auditor_session_path = f"/api/v1/sessions/{AUDITOR_ID}"
        planner_session_path = f"/api/v1/sessions/{PLANNER_ID}"
        worker_session_path = f"/api/v1/sessions/{WORKER_ID}"
        if request.method == "GET" and path == auditor_session_path:
            return httpx.Response(200, json={"session": self.auditor_session})
        if request.method == "GET" and path == planner_session_path:
            return httpx.Response(200, json={"session": self.planner_session})
        if request.method == "GET" and path == worker_session_path:
            index = min(self.worker_session_reads, len(self.worker_sessions) - 1)
            self.worker_session_reads += 1
            return httpx.Response(
                200, json={"session": self.worker_sessions[index]}
            )
        if request.method == "GET" and path == (
            f"{auditor_session_path}/workspace/files"
        ):
            index = min(self.workspace_reads, len(self.workspace_steps) - 1)
            self.workspace_reads += 1
            return httpx.Response(200, json=self.workspace_steps[index])
        if request.method == "POST" and path.endswith("/conversation/messages"):
            body = json.loads(request.content)
            self.posts.append((path, body))
            if path.startswith(auditor_session_path):
                session_id = AUDITOR_ID
                request_line = body["text"].splitlines()[0]
                self.audit_request = json.loads(
                    request_line.removeprefix("AuditRequest: ")
                )
                turn_id = "auditor-turn"
            elif path.startswith(planner_session_path):
                session_id = PLANNER_ID
                report_line = body["text"].splitlines()[0]
                self.report = json.loads(report_line.removeprefix("AuditReport: "))
                turn_id = "planner-turn"
            elif path.startswith(worker_session_path):
                session_id = WORKER_ID
                turn_id = "worker-turn"
            else:
                raise AssertionError(f"unexpected session path: {path}")

            existing = (
                body["clientMessageId"]
                in self.accepted_client_message_ids[session_id]
            )
            duplicate_mode = self.duplicate_modes.get(session_id)
            if existing or duplicate_mode is not None:
                if not existing and duplicate_mode != "missing":
                    original = self._user_message(
                        turn_id,
                        body["text"],
                    )
                    if duplicate_mode == "mismatch":
                        original["text"] = f'{body["text"]} changed'
                    elif duplicate_mode == "no_turn":
                        original.pop("turnId")
                    elif duplicate_mode == "non_human":
                        original["origin"] = "automation"
                    self.user_messages[session_id].append(original)
                    if duplicate_mode == "multiple":
                        second = dict(original)
                        second["id"] = f'{original["id"]}-second'
                        self.user_messages[session_id].append(second)
                self.accepted_client_message_ids[session_id].add(
                    body["clientMessageId"]
                )
                self.logical_turns[session_id] = 1
                return httpx.Response(
                    202,
                    json={
                        "turnId": None,
                        "providerTurnId": None,
                        "state": None,
                        "duplicate": True,
                    },
                )

            self.user_messages[session_id].append(
                self._user_message(
                    turn_id,
                    body["text"],
                )
            )
            self.accepted_client_message_ids[session_id].add(
                body["clientMessageId"]
            )
            self.logical_turns[session_id] += 1
            return httpx.Response(
                202,
                json={
                    "turnId": turn_id,
                    "providerTurnId": f"provider-{turn_id}",
                    "state": "running",
                    "duplicate": False,
                },
            )
        if request.method == "GET" and path.endswith("/conversation"):
            assert dict(request.url.params) == {"limit": "500"}
            if path.startswith(auditor_session_path):
                return self._snapshot(
                    AUDITOR_ID, "auditor-turn", self.auditor_steps
                )
            if path.startswith(planner_session_path):
                return self._snapshot(PLANNER_ID, "planner-turn", self.planner_steps)
            if path.startswith(worker_session_path):
                return self._snapshot(WORKER_ID, "worker-turn", self.worker_steps)
        raise AssertionError(f"unexpected request: {request.method} {path}")

    @staticmethod
    def _user_message(turn_id: str, text: str) -> dict[str, Any]:
        return {
            "kind": "message",
            "id": f"user-message-{turn_id}",
            "turnId": turn_id,
            "sequence": 6,
            "revision": 1,
            "role": "user",
            "origin": "human",
            "text": text,
            "streaming": False,
            "createdAt": "2026-08-28T00:00:00Z",
        }

    def _snapshot(
        self, session_id: str, turn_id: str, steps: list[Step]
    ) -> httpx.Response:
        index = min(self.polls[session_id], len(steps) - 1)
        self.polls[session_id] += 1
        step = steps[index]
        text = step.text
        if callable(text):
            if session_id == AUDITOR_ID:
                assert self.audit_request is not None
                text = text(self.audit_request)
            else:
                assert self.report is not None
                text = text(self.report)
        messages = list(self.user_messages[session_id])
        if text is not None:
            messages.append(
                {
                    "kind": "message",
                    "id": f"message-{turn_id}",
                    "turnId": turn_id,
                    "sequence": 7,
                    "revision": step.revision,
                    "role": "assistant",
                    "origin": "provider",
                    "text": text,
                    "streaming": step.streaming,
                    "createdAt": "2026-08-28T00:00:00Z",
                }
            )
        turn = {"id": turn_id, "state": step.state}
        if step.state == "failed":
            turn["errorMessage"] = "provider failed"
        return httpx.Response(
            200,
            json={
                "sessionId": session_id,
                "latestSequence": 7,
                "oldestSequence": 7,
                "hasMoreBefore": False,
                "turns": [turn],
                "messages": messages,
                "activities": [],
            },
        )


def write_runfile(path: Path) -> Path:
    path.write_text(json.dumps({"pid": 123, "port": 4567}), encoding="utf-8")
    return path


def execute(
    tmp_path: Path,
    fake: FakeAO,
    *,
    timeout: float = 2.0,
    finding: str = "the acceptance test fails",
    evidence: tuple[str, ...] = ("pytest: one failure",),
    recommended_decision: str | None = None,
    audit_id: str | None = None,
) -> Any:
    clock = FakeClock()
    runfile = write_runfile(tmp_path / "running.json")
    with AOClient(runfile, transport=httpx.MockTransport(fake.handler)) as client:
        return run_once(
            client,
            planner_session_id=PLANNER_ID,
            worker_session_id=WORKER_ID,
            finding=finding,
            audit_id=audit_id,
            evidence=evidence,
            recommended_decision=recommended_decision,
            poll_interval=0.5,
            timeout=timeout,
            _clock=clock.monotonic,
            _sleep=clock.sleep,
        )


def execute_audited(
    tmp_path: Path,
    fake: FakeAO,
    *,
    auditor_session_id: str = AUDITOR_ID,
    planner_session_id: str = PLANNER_ID,
    worker_session_id: str = WORKER_ID,
    timeout: float = 2.0,
    audit_id: str = "audit-audited",
    task_goal: str = "satisfy the task acceptance criteria",
    acceptance_criteria: Sequence[str] = (
        "all tests pass",
        "scope remains minimal",
    ),
    constraints: Sequence[str] = ("do not modify CLI",),
    evidence: Sequence[str] = ("pytest: one failure",),
) -> Any:
    clock = FakeClock()
    runfile = write_runfile(tmp_path / "running.json")
    with AOClient(runfile, transport=httpx.MockTransport(fake.handler)) as client:
        return run_audited_once(
            client,
            auditor_session_id=auditor_session_id,
            planner_session_id=planner_session_id,
            worker_session_id=worker_session_id,
            audit_id=audit_id,
            task_goal=task_goal,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            evidence=evidence,
            poll_interval=0.5,
            timeout=timeout,
            _clock=clock.monotonic,
            _sleep=clock.sleep,
        )


def gate_failure(
    *,
    duration_seconds: float = 1.0,
    exit_code: int = 7,
    stdout: str = "M4-2-GATE-FAIL\n",
) -> IntegrationGateResult:
    argv = ("python", "-c", "fail")
    return IntegrationGateResult(
        commit_sha=GATE_COMMIT,
        passed=False,
        steps=(
            GateStepResult(
                argv=argv,
                exit_code=exit_code,
                timed_out=False,
                duration_seconds=duration_seconds,
                stdout=stdout,
                stderr="",
            ),
        ),
        failure_reason=(
            f'command 1 exited with code {exit_code}: '
            '["python","-c","fail"]'
        ),
    )


def execute_gated(
    tmp_path: Path,
    fake: FakeAO,
    gate_result: IntegrationGateResult,
    *,
    audit_id: str = "root-gate-audit",
) -> GatedRunResult:
    clock = FakeClock()
    runfile = write_runfile(tmp_path / "running.json")

    def client_factory() -> AOClient:
        return AOClient(runfile, transport=httpx.MockTransport(fake.handler))

    return run_gated_once(
        client_factory,
        auditor_session_id=AUDITOR_ID,
        planner_session_id=PLANNER_ID,
        worker_session_id=WORKER_ID,
        task_goal="repair the Integration Gate failure",
        acceptance_criteria=("the Gate passes on the merged checkout",),
        constraints=("do not create a Worker",),
        evidence=("merged checkout selected",),
        audit_id=audit_id,
        gate_repo="ignored-by-fake-runner",
        gate_commands=(("python", "-c", "fail"),),
        poll_interval=0.5,
        timeout=2.0,
        _clock=clock.monotonic,
        _sleep=clock.sleep,
        _gate_runner=lambda *_args, **_kwargs: gate_result,
    )


def observed_snapshot(
    *,
    activity_state: str = "active",
    turns: tuple[TurnObservation, ...] = (TurnObservation("turn-1", "running"),),
    messages: tuple[MessageObservation, ...] = (
        MessageObservation("message-1", 1, False),
    ),
    activities: tuple[ActivityObservation, ...] = (),
    workspace_files: tuple[WorkspaceFileObservation, ...] = (),
    commit_shas: tuple[str, ...] = (),
    failures: tuple[FailureOccurrence, ...] = (),
) -> Observation:
    return Observation(
        worker_session_id=WORKER_ID,
        project_id=PROJECT_ID,
        activity_state=activity_state,
        latest_sequence=1,
        turns=turns,
        messages=messages,
        activities=activities,
        workspace_files=workspace_files,
        commit_shas=commit_shas,
        failures=failures,
    )


def observed_run_result(
    audit_id: str,
    decision: str,
    *,
    worker_response: str | None = None,
    worker_delivery_error: str | None = None,
) -> RunResult:
    local_fix = decision == "LOCAL_FIX"
    return RunResult(
        decision=PlannerDecision(
            audit_id=audit_id,
            decision=Decision(decision),
            target_session_id=WORKER_ID,
            instruction="apply the bounded fix" if local_fix else None,
            reason=f"Planner returned {decision}",
        ),
        planner_turn_id=f"planner-{audit_id}",
        planner_client_message_id=f"planner-message-{audit_id}",
        worker_turn_id=f"worker-{audit_id}" if worker_response else None,
        worker_client_message_id=(
            f"worker-message-{audit_id}" if worker_response else None
        ),
        worker_response=worker_response,
        audit_report=AuditReport(
            audit_id=audit_id,
            target_session_id=WORKER_ID,
            finding="Observer evidence requires semantic review",
            evidence=["captured from Observer"],
            recommended_decision=Decision(decision),
        ),
        auditor_turn_id=f"auditor-{audit_id}",
        auditor_client_message_id=f"auditor-message-{audit_id}",
        worker_delivery_error=worker_delivery_error,
    )


def execute_observed(
    observations: Sequence[Observation],
    audited_runner: Callable[..., RunResult],
    *,
    clock: FakeClock | None = None,
    root_audit_id: str = "root-observed",
    observe_interval: float = 1.0,
    stall_threshold: float = 300.0,
    failure_threshold: int = 2,
    max_audits: int = 3,
    overall_timeout: float = 10.0,
) -> ObservedLoopResult:
    observed_clock = clock or FakeClock()
    snapshots = iter(observations)
    return run_observed_loop(
        object(),  # type: ignore[arg-type]
        auditor_session_id=AUDITOR_ID,
        planner_session_id=PLANNER_ID,
        worker_session_id=WORKER_ID,
        task_goal="finish the observed audited task",
        acceptance_criteria=("the Planner returns PASS",),
        constraints=("do not create sessions",),
        evidence=("base CLI evidence",),
        audit_id=root_audit_id,
        observe_interval=observe_interval,
        stall_threshold=stall_threshold,
        failure_threshold=failure_threshold,
        max_audits=max_audits,
        overall_timeout=overall_timeout,
        poll_interval=0.5,
        timeout=2.0,
        _clock=observed_clock.monotonic,
        _sleep=observed_clock.sleep,
        _capture=lambda _client, _worker: next(snapshots),
        _audited_runner=audited_runner,
    )


def test_local_fix_tracks_in_place_assistant_update_and_waits_for_worker(
    tmp_path: Path,
) -> None:
    fake = FakeAO(
        planner_steps=[
            Step("running", '{"auditId":', streaming=True, revision=1),
            Step("completed", lambda report: decision_text(report), revision=2),
        ],
        worker_steps=[Step("completed", "worker applied the local fix")],
    )

    result = execute(
        tmp_path,
        fake,
        audit_id="audit-local-fix",
        evidence=("first", "second"),
        recommended_decision="LOCAL_FIX",
    )

    assert result.decision.decision == "LOCAL_FIX"
    assert result.worker_turn_id == "worker-turn"
    assert result.worker_response == "worker applied the local fix"
    assert fake.polls[PLANNER_ID] == 2
    assert len(fake.posts) == 2
    planner_body = fake.posts[0][1]
    worker_body = fake.posts[1][1]
    assert fake.report is not None
    assert fake.report["auditId"] == "audit-local-fix"
    assert fake.report["evidence"] == ["first", "second"]
    assert fake.report["recommendedDecision"] == "LOCAL_FIX"
    assert "exactly one line" in planner_body["text"]
    assert worker_body["text"] == "fix only the failing assertion"
    assert planner_body["clientMessageId"] != worker_body["clientMessageId"]
    assert result.planner_client_message_id == planner_body["clientMessageId"]
    assert result.worker_client_message_id == worker_body["clientMessageId"]
    assert all(
        message["origin"] == "human" and "clientMessageId" not in message
        for messages in fake.user_messages.values()
        for message in messages
    )


def test_default_audit_id_is_a_uuid(tmp_path: Path) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="PASS", instruction=None
                ),
            )
        ]
    )

    result = execute(tmp_path, fake)

    assert uuid.UUID(result.decision.audit_id).version == 4


@pytest.mark.parametrize("duplicate_session", [PLANNER_ID, WORKER_ID])
def test_duplicate_recovers_original_turn_for_both_stages(
    tmp_path: Path, duplicate_session: str
) -> None:
    fake = FakeAO(duplicate_modes={duplicate_session: "match"})

    result = execute(tmp_path, fake, audit_id="audit-duplicate")

    assert result.planner_turn_id == "planner-turn"
    assert result.worker_turn_id == "worker-turn"
    assert result.worker_response == "worker finished"
    assert fake.logical_turns[duplicate_session] == 1


def test_rerunning_same_audit_id_reuses_both_logical_turns(tmp_path: Path) -> None:
    fake = FakeAO()

    first = execute(tmp_path, fake, audit_id="audit-repeat")
    second = execute(tmp_path, fake, audit_id="audit-repeat")

    assert second.planner_turn_id == first.planner_turn_id
    assert second.worker_turn_id == first.worker_turn_id
    assert second.planner_client_message_id == first.planner_client_message_id
    assert second.worker_client_message_id == first.worker_client_message_id
    assert fake.logical_turns[PLANNER_ID] == 1
    assert fake.logical_turns[WORKER_ID] == 1
    assert len(fake.posts) == 4


def test_same_client_message_id_with_different_text_fails_closed(
    tmp_path: Path,
) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="PASS", instruction=None
                ),
            )
        ]
    )
    execute(tmp_path, fake, audit_id="audit-conflict", finding="first finding")

    with pytest.raises(LoopProtocolError, match="could not find"):
        execute(
            tmp_path,
            fake,
            audit_id="audit-conflict",
            finding="different finding",
        )

    assert fake.logical_turns[PLANNER_ID] == 1


@pytest.mark.parametrize("duplicate_session", [PLANNER_ID, WORKER_ID])
@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "could not find"),
        ("multiple", "multiple original"),
        ("mismatch", "could not find"),
        ("no_turn", "missing turnId"),
        ("non_human", "could not find"),
    ],
)
def test_duplicate_recovery_boundaries_fail_closed_for_both_stages(
    tmp_path: Path,
    duplicate_session: str,
    mode: str,
    message: str,
) -> None:
    fake = FakeAO(duplicate_modes={duplicate_session: mode})

    with pytest.raises(LoopProtocolError, match=message):
        execute(tmp_path, fake, audit_id=f"audit-{mode}-{duplicate_session}")

    expected_posts = 1 if duplicate_session == PLANNER_ID else 2
    assert len(fake.posts) == expected_posts


@pytest.mark.parametrize("decision", ["PASS", "REPLAN"])
def test_non_local_non_human_decisions_do_not_send_to_worker(
    tmp_path: Path, decision: str
) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision=decision, instruction=None
                ),
            )
        ]
    )

    result = execute(tmp_path, fake)

    assert result.decision.decision == decision
    assert result.worker_turn_id is None
    assert len(fake.posts) == 1


def test_human_decision_does_not_send_to_worker(tmp_path: Path) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="HUMAN", instruction=None
                ),
            )
        ]
    )

    result = execute(tmp_path, fake)

    assert result.decision.decision == "HUMAN"
    assert result.worker_response is None
    assert len(fake.posts) == 1


@pytest.mark.parametrize(
    ("text_factory", "message"),
    [
        (lambda report: "not-json", "not valid JSON"),
        (
            lambda report: decision_text(report, audit_id="wrong-audit"),
            "auditId does not match",
        ),
        (
            lambda report: decision_text(report, target_session_id="other-worker"),
            "targetSessionId does not match",
        ),
    ],
)
def test_invalid_or_mismatched_planner_decision_is_rejected(
    tmp_path: Path,
    text_factory: Callable[[dict[str, Any]], str],
    message: str,
) -> None:
    fake = FakeAO(planner_steps=[Step("completed", text_factory)])

    with pytest.raises(LoopProtocolError, match=message):
        execute(tmp_path, fake)

    assert len(fake.posts) == 1


@pytest.mark.parametrize("instruction", [None, "   "])
def test_local_fix_requires_instruction(
    tmp_path: Path, instruction: str | None
) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="LOCAL_FIX", instruction=instruction
                ),
            )
        ]
    )

    with pytest.raises(LoopProtocolError, match="requires instruction"):
        execute(tmp_path, fake)

    assert len(fake.posts) == 1


def test_planner_timeout_does_not_guess_a_decision(tmp_path: Path) -> None:
    fake = FakeAO(
        planner_steps=[Step("running", '{"partial":', streaming=True)]
    )

    with pytest.raises(LoopTimeoutError, match="Planner"):
        execute(tmp_path, fake, timeout=1.0)

    assert len(fake.posts) == 1


def test_worker_timeout_is_reported_after_local_fix_delivery(tmp_path: Path) -> None:
    fake = FakeAO(worker_steps=[Step("running", "working", streaming=True)])

    with pytest.raises(LoopTimeoutError, match="Worker"):
        execute(tmp_path, fake, timeout=1.0)

    assert len(fake.posts) == 2


@pytest.mark.parametrize("failed_role", ["planner", "worker"])
def test_failed_turn_is_not_treated_as_a_response(
    tmp_path: Path, failed_role: str
) -> None:
    fake = FakeAO(
        planner_steps=[Step("failed")]
        if failed_role == "planner"
        else None,
        worker_steps=[Step("failed")]
        if failed_role == "worker"
        else None,
    )

    with pytest.raises(TurnFailedError, match="provider failed"):
        execute(tmp_path, fake)

    assert len(fake.posts) == (1 if failed_role == "planner" else 2)


@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("planner", {"kind": "worker"}),
        ("planner", {"mode": "tui"}),
        ("planner", {"isTerminated": True}),
        ("planner", {"activity": {"state": "blocked"}}),
        ("worker", {"activity": {"state": "exited"}}),
        ("worker", {"activity": {"state": "active"}}),
    ],
)
def test_unsafe_session_blocks_all_delivery(
    tmp_path: Path, target: str, changes: dict[str, Any]
) -> None:
    fake = FakeAO()
    if target == "planner":
        fake.planner_session.update(changes)
    else:
        fake.worker_sessions[0].update(changes)

    with pytest.raises(UnsafeSessionError):
        execute(tmp_path, fake)

    assert fake.posts == []


def test_sessions_from_different_projects_block_all_delivery(tmp_path: Path) -> None:
    fake = FakeAO()
    fake.worker_sessions[0]["projectId"] = "project-2"

    with pytest.raises(UnsafeSessionError, match="same AO Project"):
        execute(tmp_path, fake)

    assert fake.posts == []


@pytest.mark.parametrize(
    ("target", "project_id"),
    [
        ("planner", None),
        ("planner", 123),
        ("worker", ""),
        ("worker", "   "),
    ],
)
def test_missing_or_invalid_project_id_blocks_all_delivery(
    tmp_path: Path, target: str, project_id: object
) -> None:
    fake = FakeAO()
    session = fake.planner_session if target == "planner" else fake.worker_sessions[0]
    if project_id is None:
        session.pop("projectId")
    else:
        session["projectId"] = project_id

    with pytest.raises(UnsafeSessionError, match="non-empty projectId"):
        execute(tmp_path, fake)

    assert fake.posts == []


def test_worker_is_gated_again_immediately_before_local_fix(tmp_path: Path) -> None:
    fake = FakeAO()
    blocked = safe_session(WORKER_ID, "worker")
    blocked["activity"] = {"state": "blocked"}
    fake.worker_sessions = [safe_session(WORKER_ID, "worker"), blocked]

    with pytest.raises(UnsafeSessionError):
        execute(tmp_path, fake)

    assert len(fake.posts) == 1


def test_worker_project_is_gated_again_before_local_fix(tmp_path: Path) -> None:
    fake = FakeAO()
    moved = safe_session(WORKER_ID, "worker")
    moved["projectId"] = "project-2"
    fake.worker_sessions = [safe_session(WORKER_ID, "worker"), moved]

    with pytest.raises(UnsafeSessionError, match="initially validated AO Project"):
        execute(tmp_path, fake)

    assert len(fake.posts) == 1
    assert fake.posts[0][0].startswith(f"/api/v1/sessions/{PLANNER_ID}")


def test_waiting_input_is_an_instruction_safe_idle_state(tmp_path: Path) -> None:
    fake = FakeAO(
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="PASS", instruction=None
                ),
            )
        ]
    )
    fake.planner_session["activity"] = {"state": "waiting_input"}
    fake.worker_sessions[0]["activity"] = {"state": "waiting_input"}

    result = execute(tmp_path, fake)

    assert result.decision.decision == "PASS"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"acceptance_criteria": ()}, "at least one item"),
        ({"acceptance_criteria": ("   ",)}, "acceptance_criteria item"),
        ({"task_goal": " \t "}, "task_goal"),
        ({"acceptance_criteria": "criterion"}, "acceptance_criteria"),
        ({"acceptance_criteria": b"criterion"}, "acceptance_criteria"),
        ({"constraints": "constraint"}, "constraints"),
        ({"constraints": b"constraint"}, "constraints"),
        ({"evidence": "evidence"}, "evidence"),
        ({"evidence": b"evidence"}, "evidence"),
    ],
)
def test_invalid_audit_request_input_is_rejected_before_any_ao_call(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    fake = FakeAO()

    with pytest.raises(ValueError, match=message):
        execute_audited(tmp_path, fake, **overrides)

    assert fake.requests == []


def test_audited_loop_allows_empty_constraints_and_evidence(
    tmp_path: Path,
) -> None:
    fake = FakeAO()

    result = execute_audited(
        tmp_path,
        fake,
        constraints=(),
        evidence=(),
    )

    assert result.decision.decision == "LOCAL_FIX"
    assert fake.audit_request is not None
    assert fake.audit_request["constraints"] == []
    assert fake.audit_request["evidence"] == []


def test_auditor_planner_worker_local_fix_closed_loop(tmp_path: Path) -> None:
    fake = FakeAO(
        auditor_steps=[
            Step("completed", lambda request: audit_report_text(request))
        ],
        planner_steps=[
            Step("completed", lambda report: decision_text(report))
        ],
        worker_steps=[Step("completed", "worker applied audited fix")],
    )

    result = execute_audited(tmp_path, fake, audit_id="audit-m2-local-fix")

    assert result.decision.decision == "LOCAL_FIX"
    assert result.worker_response == "worker applied audited fix"
    assert result.audit_report is not None
    assert result.audit_report.audit_id == "audit-m2-local-fix"
    assert result.audit_report.recommended_decision == "LOCAL_FIX"
    assert result.auditor_turn_id == "auditor-turn"
    assert result.auditor_client_message_id == fake.posts[0][1][
        "clientMessageId"
    ]
    assert result.as_dict()["auditReport"]["recommendedDecision"] == (
        "LOCAL_FIX"
    )
    assert fake.workspace_reads == 2
    assert len(fake.posts) == 3
    assert [path for path, _ in fake.posts] == [
        f"/api/v1/sessions/{AUDITOR_ID}/conversation/messages",
        f"/api/v1/sessions/{PLANNER_ID}/conversation/messages",
        f"/api/v1/sessions/{WORKER_ID}/conversation/messages",
    ]
    assert fake.audit_request == {
        "auditId": "audit-m2-local-fix",
        "targetSessionId": WORKER_ID,
        "taskGoal": "satisfy the task acceptance criteria",
        "acceptanceCriteria": ["all tests pass", "scope remains minimal"],
        "constraints": ["do not modify CLI"],
        "evidence": ["pytest: one failure"],
    }
    auditor_prompt = fake.posts[0][1]["text"]
    assert "exactly one line" in auditor_prompt
    assert "read-only Auditor" in auditor_prompt
    for forbidden_action in ("modify files", "create commits", "schedule"):
        assert forbidden_action in auditor_prompt
    assert fake.report is not None
    assert fake.report["recommendedDecision"] == "LOCAL_FIX"


def test_auditor_pass_still_reaches_planner_but_not_worker(tmp_path: Path) -> None:
    fake = FakeAO(
        auditor_steps=[
            Step(
                "completed",
                lambda request: audit_report_text(
                    request, recommended_decision="PASS"
                ),
            )
        ],
        planner_steps=[
            Step(
                "completed",
                lambda report: decision_text(
                    report, decision="PASS", instruction=None
                ),
            )
        ],
    )

    result = execute_audited(tmp_path, fake)

    assert result.decision.decision == "PASS"
    assert result.worker_turn_id is None
    assert len(fake.posts) == 2
    assert fake.report is not None
    assert fake.report["recommendedDecision"] == "PASS"


@pytest.mark.parametrize(
    ("text_factory", "message"),
    [
        (lambda request: "not-json", "not valid JSON"),
        (
            lambda request: audit_report_text(
                request, audit_id="wrong-audit"
            ),
            "auditId does not match",
        ),
        (
            lambda request: audit_report_text(
                request, target_session_id="other-worker"
            ),
            "targetSessionId does not match",
        ),
        (
            lambda request: audit_report_text(
                request, recommended_decision=None
            ),
            "requires recommendedDecision",
        ),
    ],
)
def test_invalid_or_unrelated_auditor_report_is_rejected(
    tmp_path: Path,
    text_factory: Callable[[dict[str, Any]], str],
    message: str,
) -> None:
    fake = FakeAO(auditor_steps=[Step("completed", text_factory)])

    with pytest.raises(LoopProtocolError, match=message):
        execute_audited(tmp_path, fake)

    assert len(fake.posts) == 1
    assert fake.workspace_reads == 2


@pytest.mark.parametrize("target", ["auditor", "planner", "worker"])
def test_audited_sessions_from_different_projects_block_delivery(
    tmp_path: Path, target: str
) -> None:
    fake = FakeAO()
    session = {
        "auditor": fake.auditor_session,
        "planner": fake.planner_session,
        "worker": fake.worker_sessions[0],
    }[target]
    session["projectId"] = "project-2"

    with pytest.raises(UnsafeSessionError, match="same AO Project"):
        execute_audited(tmp_path, fake)

    assert fake.posts == []


@pytest.mark.parametrize(
    ("auditor_id", "planner_id", "worker_id"),
    [
        (AUDITOR_ID, AUDITOR_ID, WORKER_ID),
        (AUDITOR_ID, WORKER_ID, WORKER_ID),
        (PLANNER_ID, PLANNER_ID, PLANNER_ID),
    ],
)
def test_audited_session_ids_must_be_distinct(
    tmp_path: Path, auditor_id: str, planner_id: str, worker_id: str
) -> None:
    fake = FakeAO()

    with pytest.raises(UnsafeSessionError, match="must be different"):
        execute_audited(
            tmp_path,
            fake,
            auditor_session_id=auditor_id,
            planner_session_id=planner_id,
            worker_session_id=worker_id,
        )

    assert fake.posts == []


def test_auditor_must_be_a_safe_worker_session(tmp_path: Path) -> None:
    fake = FakeAO()
    fake.auditor_session["kind"] = "orchestrator"

    with pytest.raises(UnsafeSessionError, match="kind 'worker'"):
        execute_audited(tmp_path, fake)

    assert fake.posts == []


def test_auditor_workspace_must_be_clean_before_execution(tmp_path: Path) -> None:
    dirty = clean_workspace()
    dirty["files"] = [{"path": "changed.py", "status": "modified"}]
    fake = FakeAO(workspace_steps=[dirty])

    with pytest.raises(UnsafeSessionError, match="before execution"):
        execute_audited(tmp_path, fake)

    assert fake.workspace_reads == 1
    assert fake.posts == []


def test_auditor_workspace_allows_tracked_unmodified_files(tmp_path: Path) -> None:
    workspace = clean_workspace()
    workspace["files"] = [
        {
            "path": "README.md",
            "status": "unmodified",
            "additions": 0,
            "deletions": 0,
        }
    ]
    fake = FakeAO(workspace_steps=[workspace])

    result = execute_audited(tmp_path, fake)

    assert result.decision.decision == "LOCAL_FIX"
    assert fake.workspace_reads == 2


@pytest.mark.parametrize("mutation", ["file", "commit"])
def test_auditor_workspace_changes_after_execution_are_rejected(
    tmp_path: Path, mutation: str
) -> None:
    changed = clean_workspace()
    if mutation == "file":
        changed["files"] = [{"path": "changed.py", "status": "modified"}]
    else:
        changed["commits"] = [{"sha": "new-commit"}]
    fake = FakeAO(workspace_steps=[clean_workspace(), changed])

    with pytest.raises(UnsafeSessionError, match="after execution"):
        execute_audited(tmp_path, fake)

    assert fake.workspace_reads == 2
    assert len(fake.posts) == 1


def test_blocked_auditor_blocks_all_delivery(tmp_path: Path) -> None:
    fake = FakeAO()
    fake.auditor_session["activity"] = {"state": "blocked"}

    with pytest.raises(UnsafeSessionError):
        execute_audited(tmp_path, fake)

    assert fake.posts == []


def test_auditor_timeout_does_not_reach_planner(tmp_path: Path) -> None:
    fake = FakeAO(
        auditor_steps=[Step("running", '{"partial":', streaming=True)]
    )

    with pytest.raises(LoopTimeoutError, match="Auditor"):
        execute_audited(tmp_path, fake, timeout=1.0)

    assert len(fake.posts) == 1
    assert fake.workspace_reads == 2


def test_failed_auditor_turn_does_not_reach_planner(tmp_path: Path) -> None:
    fake = FakeAO(auditor_steps=[Step("failed")])

    with pytest.raises(TurnFailedError, match="provider failed"):
        execute_audited(tmp_path, fake)

    assert len(fake.posts) == 1
    assert fake.workspace_reads == 2


def test_observed_loop_without_trigger_reaches_overall_timeout_as_human() -> None:
    unchanged = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("existing-turn", "completed"),),
    )

    result = execute_observed(
        [unchanged, unchanged, unchanged],
        lambda *_args, **_kwargs: pytest.fail("Auditor must not run"),
        overall_timeout=3.0,
    )

    assert result.termination is Decision.HUMAN
    assert result.audit_count == 0
    assert result.rounds == ()
    assert "overall observation timeout" in result.reason


def test_observed_loop_runs_milestone_local_fix_new_milestone_pass() -> None:
    initial = observed_snapshot()
    first_milestone = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )
    second_milestone = observed_snapshot(
        activity_state="idle",
        turns=(
            TurnObservation("turn-1", "completed"),
            TurnObservation("turn-2", "completed"),
        ),
        messages=(
            MessageObservation("message-1", 1, False),
            MessageObservation("message-2", 1, False),
        ),
    )
    calls: list[dict[str, Any]] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        calls.append(kwargs)
        if len(calls) == 1:
            return observed_run_result(
                kwargs["audit_id"],
                "LOCAL_FIX",
                worker_response="M3-2-WORKER-ACK",
            )
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [initial, first_milestone, second_milestone], audited_runner
    )

    assert result.termination is Decision.PASS
    assert result.audit_count == 2
    assert [item["observer"]["trigger"] for item in result.rounds] == [
        "MILESTONE",
        "MILESTONE",
    ]
    assert result.rounds[0]["workerResponse"] == "M3-2-WORKER-ACK"
    assert result.rounds[1]["plannerDecision"]["decision"] == "PASS"
    assert result.rounds[1]["turns"]["auditor"]["turnId"].startswith(
        "auditor-"
    )
    assert "base CLI evidence" in calls[0]["evidence"]
    assert "Observer trigger: MILESTONE" in calls[0]["evidence"]
    assert (
        "Previous LOCAL_FIX worker response: M3-2-WORKER-ACK"
        in calls[1]["evidence"]
    )
    assert calls[0]["_allow_active_worker"] is True


def test_observed_loop_repeated_failure_automatically_runs_auditor() -> None:
    initial = observed_snapshot(
        failures=(FailureOccurrence("turn:turn-1", "same failure"),)
    )
    repeated = observed_snapshot(
        failures=(
            FailureOccurrence("turn:turn-1", "same failure"),
            FailureOccurrence("activity:activity-2", "same failure"),
        )
    )

    result = execute_observed(
        [initial, repeated],
        lambda _client, **kwargs: observed_run_result(
            kwargs["audit_id"], "PASS"
        ),
    )

    assert result.termination is Decision.PASS
    assert result.rounds[0]["observer"]["trigger"] == "REPEATED_FAILURE"
    assert "failure fingerprint: same failure" in result.rounds[0][
        "observer"
    ]["evidence"]


def test_observed_loop_stall_on_active_worker_runs_auditor() -> None:
    active = observed_snapshot()
    calls: list[dict[str, Any]] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        calls.append(kwargs)
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [active, active], audited_runner, stall_threshold=1.0
    )

    assert result.termination is Decision.PASS
    assert result.rounds[0]["observer"]["trigger"] == "STALL"
    assert calls[0]["_allow_active_worker"] is True


def test_idle_worker_becoming_active_does_not_immediately_stall() -> None:
    idle = observed_snapshot(activity_state="idle")
    active = observed_snapshot()

    result = execute_observed(
        [idle, idle, idle, idle, active, active],
        lambda *_args, **_kwargs: pytest.fail("Auditor must not run"),
        stall_threshold=3.0,
        overall_timeout=6.0,
    )

    assert result.termination is Decision.HUMAN
    assert result.audit_count == 0
    assert "overall observation timeout" in result.reason


def test_idle_to_active_restarts_full_stall_threshold() -> None:
    clock = FakeClock()
    idle = observed_snapshot(activity_state="idle")
    active = observed_snapshot()
    audit_times: list[float] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        audit_times.append(clock.monotonic())
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [idle, idle, idle, active, active, active],
        audited_runner,
        clock=clock,
        stall_threshold=2.0,
        overall_timeout=6.0,
    )

    assert result.rounds[0]["observer"]["trigger"] == "STALL"
    assert audit_times == [5.0]


def test_active_idle_active_restarts_stall_threshold_again() -> None:
    clock = FakeClock()
    active = observed_snapshot()
    idle = observed_snapshot(activity_state="idle")
    audit_times: list[float] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        audit_times.append(clock.monotonic())
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [active, active, idle, active, active, active],
        audited_runner,
        clock=clock,
        stall_threshold=2.0,
        overall_timeout=6.0,
    )

    assert result.rounds[0]["observer"]["trigger"] == "STALL"
    assert audit_times == [5.0]


def test_continuous_active_without_progress_stalls_at_threshold() -> None:
    clock = FakeClock()
    active = observed_snapshot()
    audit_times: list[float] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        audit_times.append(clock.monotonic())
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [active, active, active],
        audited_runner,
        clock=clock,
        stall_threshold=2.0,
    )

    assert result.rounds[0]["observer"]["trigger"] == "STALL"
    assert audit_times == [2.0]


@pytest.mark.parametrize(
    "changed",
    [
        observed_snapshot(
            messages=(MessageObservation("message-1", 2, False),)
        ),
        observed_snapshot(
            activities=(
                ActivityObservation("activity-1", 1, "command", "running"),
            )
        ),
        observed_snapshot(
            workspace_files=(WorkspaceFileObservation("src/app.py", "modified"),)
        ),
        observed_snapshot(commit_shas=("abc123",)),
    ],
    ids=["message", "activity", "workspace", "commit"],
)
def test_observed_progress_change_resets_stall_origin(
    changed: Observation,
) -> None:
    clock = FakeClock()
    audit_times: list[float] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        audit_times.append(clock.monotonic())
        return observed_run_result(kwargs["audit_id"], "PASS")

    result = execute_observed(
        [observed_snapshot(), changed, changed, changed],
        audited_runner,
        clock=clock,
        stall_threshold=2.0,
        overall_timeout=5.0,
    )

    assert result.rounds[0]["observer"]["trigger"] == "STALL"
    assert audit_times == [3.0]


@pytest.mark.parametrize("decision", ["PASS", "REPLAN", "HUMAN"])
def test_observed_terminal_planner_decisions_stop_immediately(
    decision: str,
) -> None:
    milestone = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )
    calls = 0

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        nonlocal calls
        calls += 1
        return observed_run_result(kwargs["audit_id"], decision)

    result = execute_observed(
        [observed_snapshot(), milestone], audited_runner
    )

    assert result.termination.value == decision
    assert result.audit_count == 1
    assert calls == 1


def test_observed_max_audits_turns_next_trigger_into_human() -> None:
    first = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )
    second = observed_snapshot(
        activity_state="idle",
        turns=(
            TurnObservation("turn-1", "completed"),
            TurnObservation("turn-2", "completed"),
        ),
    )

    result = execute_observed(
        [observed_snapshot(), first, second],
        lambda _client, **kwargs: observed_run_result(
            kwargs["audit_id"],
            "LOCAL_FIX",
            worker_response="worker response",
        ),
        max_audits=1,
    )

    assert result.termination is Decision.HUMAN
    assert result.audit_count == 1
    assert len(result.rounds) == 1
    assert "maximum audit count 1" in result.reason


def test_same_root_and_observation_generate_same_cycle_audit_id() -> None:
    milestone = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )
    cycle_ids: list[str] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        cycle_ids.append(kwargs["audit_id"])
        return observed_run_result(kwargs["audit_id"], "PASS")

    for _ in range(2):
        execute_observed(
            [observed_snapshot(), milestone],
            audited_runner,
            root_audit_id="stable-root",
        )

    assert cycle_ids[0] == cycle_ids[1]
    assert cycle_ids[0].startswith("stable-root:")


def test_activity_state_changes_cycle_id_for_same_progress_signature() -> None:
    completed_turn = (TurnObservation("turn-1", "completed"),)
    current_observations = [
        observed_snapshot(activity_state=state, turns=completed_turn)
        for state in ("idle", "waiting_input")
    ]
    cycle_ids: list[str] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        cycle_ids.append(kwargs["audit_id"])
        return observed_run_result(kwargs["audit_id"], "PASS")

    assert (
        current_observations[0].progress_signature
        == current_observations[1].progress_signature
    )
    for current in current_observations:
        execute_observed(
            [observed_snapshot(), current],
            audited_runner,
            root_audit_id="activity-sensitive-root",
        )

    assert cycle_ids[0] != cycle_ids[1]


def test_same_stall_at_different_elapsed_has_same_cycle_and_audit_evidence() -> None:
    active = observed_snapshot()
    calls: list[dict[str, Any]] = []

    def audited_runner(_client: Any, **kwargs: Any) -> RunResult:
        calls.append(kwargs)
        return observed_run_result(kwargs["audit_id"], "PASS")

    for observe_interval in (2.0, 3.0):
        execute_observed(
            [active, active],
            audited_runner,
            root_audit_id="stable-stall-root",
            observe_interval=observe_interval,
            stall_threshold=2.0,
        )

    assert calls[0]["audit_id"] == calls[1]["audit_id"]
    assert calls[0]["evidence"] == calls[1]["evidence"]


def test_observed_rerun_reuses_existing_three_stage_turns(tmp_path: Path) -> None:
    fake = FakeAO()
    runfile = write_runfile(tmp_path / "running.json")
    stalled = observed_snapshot()
    progressed = observed_snapshot(
        messages=(MessageObservation("message-1", 2, False),),
    )
    cycle_ids: list[str] = []
    results: list[ObservedLoopResult] = []
    with AOClient(runfile, transport=httpx.MockTransport(fake.handler)) as client:
        for observe_interval in (2.0, 3.0):
            clock = FakeClock()
            snapshots = iter([stalled, stalled, progressed])
            result = run_observed_loop(
                client,
                auditor_session_id=AUDITOR_ID,
                planner_session_id=PLANNER_ID,
                worker_session_id=WORKER_ID,
                task_goal="verify deterministic recovery",
                acceptance_criteria=("reuse all three stages",),
                audit_id="stable-rerun-root",
                observe_interval=observe_interval,
                stall_threshold=2.0,
                overall_timeout=observe_interval + 1.0,
                poll_interval=0.5,
                timeout=1.0,
                _clock=clock.monotonic,
                _sleep=clock.sleep,
                _capture=lambda _client, _worker: next(snapshots),
            )
            cycle_ids.append(result.rounds[0]["cycleAuditId"])
            results.append(result)

    assert cycle_ids[0] == cycle_ids[1]
    assert results[0].rounds[0]["observer"] == results[1].rounds[0]["observer"]
    assert fake.posts[0][1]["text"] == fake.posts[3][1]["text"]
    assert fake.logical_turns == {
        AUDITOR_ID: 1,
        PLANNER_ID: 1,
        WORKER_ID: 1,
    }


def test_active_worker_is_audited_then_waited_until_safe_for_local_fix(
    tmp_path: Path,
) -> None:
    fake = FakeAO()
    active = safe_session(WORKER_ID, "worker")
    active["activity"] = {"state": "active"}
    fake.worker_sessions = [
        active,
        active,
        active,
        active,
        safe_session(WORKER_ID, "worker"),
    ]
    clock = FakeClock()
    runfile = write_runfile(tmp_path / "running.json")

    with AOClient(runfile, transport=httpx.MockTransport(fake.handler)) as client:
        result = run_audited_once(
            client,
            auditor_session_id=AUDITOR_ID,
            planner_session_id=PLANNER_ID,
            worker_session_id=WORKER_ID,
            task_goal="audit while Worker is active",
            acceptance_criteria=("deliver only after idle",),
            audit_id="active-worker-audit",
            poll_interval=0.5,
            timeout=2.0,
            _clock=clock.monotonic,
            _sleep=clock.sleep,
            _allow_active_worker=True,
        )

    assert result.worker_response == "worker finished"
    assert len(fake.posts) == 3
    assert fake.posts[-1][0].startswith(f"/api/v1/sessions/{WORKER_ID}")
    assert clock.monotonic() == 0.5


@pytest.mark.parametrize("state", ["blocked", "exited", "terminated"])
def test_observed_local_fix_never_delivers_to_prohibited_worker_state(
    tmp_path: Path, state: str
) -> None:
    fake = FakeAO()
    active = safe_session(WORKER_ID, "worker")
    active["activity"] = {"state": "active"}
    prohibited = safe_session(WORKER_ID, "worker")
    if state == "terminated":
        prohibited["isTerminated"] = True
    else:
        prohibited["activity"] = {"state": state}
    fake.worker_sessions = [active, active, prohibited]
    clock = FakeClock()
    runfile = write_runfile(tmp_path / "running.json")

    with AOClient(runfile, transport=httpx.MockTransport(fake.handler)) as client:
        result = run_audited_once(
            client,
            auditor_session_id=AUDITOR_ID,
            planner_session_id=PLANNER_ID,
            worker_session_id=WORKER_ID,
            task_goal="never inject into an unsafe Worker",
            acceptance_criteria=("no Worker delivery",),
            audit_id=f"prohibited-{state}",
            poll_interval=0.5,
            timeout=1.0,
            _clock=clock.monotonic,
            _sleep=clock.sleep,
            _allow_active_worker=True,
        )

    assert result.decision.decision is Decision.LOCAL_FIX
    assert result.worker_delivery_error is not None
    assert result.worker_turn_id is None
    assert len(fake.posts) == 2


def test_observed_delivery_timeout_becomes_human() -> None:
    milestone = observed_snapshot(
        activity_state="idle",
        turns=(TurnObservation("turn-1", "completed"),),
    )

    result = execute_observed(
        [observed_snapshot(), milestone],
        lambda _client, **kwargs: observed_run_result(
            kwargs["audit_id"],
            "LOCAL_FIX",
            worker_delivery_error=(
                "timed out waiting for Worker to become safe for LOCAL_FIX"
            ),
        ),
    )

    assert result.termination is Decision.HUMAN
    assert "timed out waiting" in result.reason
    assert result.rounds[0]["error"] == result.reason


def test_gated_pass_skips_client_and_all_agent_feedback() -> None:
    client_calls = 0
    gate_result = IntegrationGateResult(
        commit_sha=GATE_COMMIT,
        passed=True,
        steps=(
            GateStepResult(
                argv=("python", "-m", "pytest"),
                exit_code=0,
                timed_out=False,
                duration_seconds=2.5,
                stdout="282 passed\n",
                stderr="",
            ),
        ),
        failure_reason=None,
    )

    def forbidden_client_factory() -> AOClient:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("AOClient must not be created for a passing Gate")

    result = run_gated_once(
        forbidden_client_factory,
        auditor_session_id=AUDITOR_ID,
        planner_session_id=PLANNER_ID,
        worker_session_id=WORKER_ID,
        task_goal="verify integration",
        acceptance_criteria=("Gate passes",),
        audit_id="root-pass",
        gate_repo="ignored",
        gate_commands=(("python", "-m", "pytest"),),
        _gate_runner=lambda *_args, **_kwargs: gate_result,
    )

    assert client_calls == 0
    assert result.gate_result is gate_result
    assert result.gate_audit_id is None
    assert result.audited_result is None
    assert result.feedback_skipped_reason is not None
    assert result.as_dict()["auditedResult"] is None
    assert result.as_dict()["gate"] == gate_result.to_dict()


def test_gated_precondition_failure_skips_client_and_marks_reason() -> None:
    client_calls = 0
    gate_result = IntegrationGateResult(
        commit_sha=GATE_COMMIT,
        passed=False,
        steps=(),
        failure_reason="Git working tree is not clean:  M src/example.py",
    )

    def forbidden_client_factory() -> AOClient:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("AOClient must not be created for precondition failure")

    result = run_gated_once(
        forbidden_client_factory,
        auditor_session_id=AUDITOR_ID,
        planner_session_id=PLANNER_ID,
        worker_session_id=WORKER_ID,
        task_goal="verify integration",
        acceptance_criteria=("Gate passes",),
        audit_id="root-precondition",
        gate_repo="ignored",
        gate_commands=(("python", "-m", "pytest"),),
        _gate_runner=lambda *_args, **_kwargs: gate_result,
    )

    assert client_calls == 0
    assert result.audited_result is None
    assert result.gate_audit_id is None
    assert "precondition failed" in result.feedback_skipped_reason


def test_gated_command_failure_runs_auditor_planner_and_worker(
    tmp_path: Path,
) -> None:
    fake = FakeAO(worker_steps=[Step("completed", "M4-2-GATE-ACK")])

    result = execute_gated(tmp_path, fake, gate_failure())

    assert result.gate_audit_id is not None
    assert result.gate_audit_id.startswith("root-gate-audit:")
    assert result.audited_result is not None
    assert result.audited_result.decision.decision is Decision.LOCAL_FIX
    assert result.audited_result.worker_response == "M4-2-GATE-ACK"
    assert [path for path, _body in fake.posts] == [
        f"/api/v1/sessions/{AUDITOR_ID}/conversation/messages",
        f"/api/v1/sessions/{PLANNER_ID}/conversation/messages",
        f"/api/v1/sessions/{WORKER_ID}/conversation/messages",
    ]
    assert fake.audit_request is not None
    gate_evidence = fake.audit_request["evidence"][-1]
    assert f"integration_gate.commit_sha={GATE_COMMIT}" in gate_evidence
    assert "integration_gate.step[1].exit_code=7" in gate_evidence
    assert "integration_gate.step[1].stdout=M4-2-GATE-FAIL\n" in gate_evidence
    assert "duration_seconds" not in gate_evidence
    assert result.as_dict()["auditedResult"]["workerResponse"] == (
        "M4-2-GATE-ACK"
    )


def test_identical_gated_failure_reuses_all_three_existing_turns(
    tmp_path: Path,
) -> None:
    fake = FakeAO(worker_steps=[Step("completed", "M4-2-GATE-ACK")])

    first = execute_gated(
        tmp_path,
        fake,
        gate_failure(duration_seconds=0.25),
        audit_id="stable-root",
    )
    second = execute_gated(
        tmp_path,
        fake,
        gate_failure(duration_seconds=99.0),
        audit_id="stable-root",
    )

    assert first.gate_audit_id == second.gate_audit_id
    assert first.audited_result is not None
    assert second.audited_result is not None
    assert first.audited_result.auditor_turn_id == (
        second.audited_result.auditor_turn_id
    )
    assert first.audited_result.planner_turn_id == (
        second.audited_result.planner_turn_id
    )
    assert first.audited_result.worker_turn_id == (
        second.audited_result.worker_turn_id
    )
    assert fake.logical_turns == {
        AUDITOR_ID: 1,
        PLANNER_ID: 1,
        WORKER_ID: 1,
    }


def test_gate_evidence_change_changes_gate_audit_id(tmp_path: Path) -> None:
    first = execute_gated(
        tmp_path,
        FakeAO(),
        gate_failure(exit_code=7),
        audit_id="changed-evidence-root",
    )
    second = execute_gated(
        tmp_path,
        FakeAO(),
        gate_failure(exit_code=8),
        audit_id="changed-evidence-root",
    )

    assert first.gate_audit_id != second.gate_audit_id
