from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from typing import Any

import pytest

from closed_loop_agent_orchestrator.cli import main
from closed_loop_agent_orchestrator.integration_gate import (
    GateStepResult,
    IntegrationGateResult,
)
from closed_loop_agent_orchestrator.loop_runner import (
    GatedRunResult,
    LoopProtocolError,
    ObservedLoopResult,
    PlannerDecision,
    RunResult,
)
from closed_loop_agent_orchestrator.protocol import AuditReport, Decision


class FakeClient:
    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def client_factory(runfile: str | None) -> FakeClient:
    return FakeClient()


def result_for(decision: str, *, audit_id: str = "audit-1") -> RunResult:
    return RunResult(
        decision=PlannerDecision(
            audit_id=audit_id,
            decision=Decision(decision),
            target_session_id="worker-1",
            reason="test result",
        ),
        planner_turn_id="planner-turn",
        planner_client_message_id="planner-message",
    )


def audited_result_for(
    decision: str, *, audit_id: str = "audit-audited"
) -> RunResult:
    return RunResult(
        decision=PlannerDecision(
            audit_id=audit_id,
            decision=Decision(decision),
            target_session_id="worker-1",
            reason="audited test result",
        ),
        planner_turn_id="planner-turn",
        planner_client_message_id="planner-message",
        audit_report=AuditReport(
            audit_id=audit_id,
            target_session_id="worker-1",
            finding="acceptance evidence is insufficient",
            evidence=["Auditor returned READY"],
            recommended_decision=Decision.LOCAL_FIX,
        ),
        auditor_turn_id="auditor-turn",
        auditor_client_message_id="auditor-message",
    )


def observed_result_for(
    termination: str, *, audit_id: str = "root-observed"
) -> ObservedLoopResult:
    return ObservedLoopResult(
        root_audit_id=audit_id,
        termination=Decision(termination),
        reason=f"observed loop returned {termination}",
        rounds=(),
        audit_count=0,
    )


def gated_result_for(
    *,
    passed: bool,
    decision: str | None = None,
    precondition_failure: bool = False,
) -> GatedRunResult:
    steps = ()
    if not precondition_failure:
        steps = (
            GateStepResult(
                argv=("python", "-m", "pytest"),
                exit_code=0 if passed else 7,
                timed_out=False,
                duration_seconds=1.25,
                stdout="tests completed\n",
                stderr="" if passed else "integration failure\n",
            ),
        )
    gate_result = IntegrationGateResult(
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        passed=passed,
        steps=steps,
        failure_reason=(
            None
            if passed
            else (
                "Git working tree is not clean"
                if precondition_failure
                else "command 1 exited with code 7"
            )
        ),
    )
    audited_result = (
        None
        if decision is None
        else audited_result_for(
            decision,
            audit_id="root-gate:deterministic",
        )
    )
    return GatedRunResult(
        gate_result=gate_result,
        gate_audit_id=(
            None if audited_result is None else "root-gate:deterministic"
        ),
        audited_result=audited_result,
        feedback_skipped_reason=(
            "Integration Gate precondition failed"
            if precondition_failure
            else (
                "Integration Gate passed; Agent feedback was not required"
                if passed
                else None
            )
        ),
    )


def base_args() -> list[str]:
    return [
        "--planner-session",
        "planner-1",
        "--worker-session",
        "worker-1",
        "--finding",
        "acceptance failed",
    ]


def audited_args() -> list[str]:
    return [
        "--planner-session",
        "planner-1",
        "--worker-session",
        "worker-1",
        "--auditor-session",
        "auditor-1",
        "--task-goal",
        "verify the audited loop",
        "--acceptance-criterion",
        "Auditor returns an AuditReport",
    ]


def observed_args() -> list[str]:
    return audited_args() + [
        "--observe",
        "--audit-id",
        "root-observed",
    ]


def gate_args() -> list[str]:
    return audited_args() + [
        "--gate",
        "--audit-id",
        "root-gate",
        "--gate-repo",
        "merged-main",
        "--gate-command-json",
        '["python","-m","pytest"]',
    ]


@pytest.mark.parametrize(("decision", "expected_code"), [("PASS", 0), ("HUMAN", 2)])
def test_cli_writes_json_and_maps_valid_result_exit_codes(
    capsys: pytest.CaptureFixture[str], decision: str, expected_code: int
) -> None:
    def runner(client: Any, **kwargs: Any) -> RunResult:
        return result_for(decision)

    code = main(
        base_args(), client_factory=client_factory, runner=runner
    )

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["decision"] == decision
    assert "auditReport" not in payload
    assert "auditorTurnId" not in payload
    assert "auditorClientMessageId" not in payload
    assert captured.out.count("\n") == 1


def test_cli_passes_repeatable_evidence_and_optional_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def runner(client: Any, **kwargs: Any) -> RunResult:
        seen.update(kwargs)
        return result_for("REPLAN", audit_id=kwargs["audit_id"])

    code = main(
        base_args()
        + [
            "--evidence",
            "first",
            "--evidence",
            "second",
            "--audit-id",
            "audit-cli",
            "--recommended-decision",
            "REPLAN",
            "--poll-interval",
            "0.25",
            "--timeout",
            "12",
            "--runfile",
            "relative-runfile.json",
        ],
        client_factory=client_factory,
        runner=runner,
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["decision"] == "REPLAN"
    assert output["auditId"] == "audit-cli"
    assert seen["audit_id"] == "audit-cli"
    assert seen["evidence"] == ["first", "second"]
    assert seen["recommended_decision"] == "REPLAN"
    assert seen["poll_interval"] == 0.25
    assert seen["timeout"] == 12.0


def test_audited_cli_passes_repeatable_contract_arguments_and_outputs_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}
    runfiles: list[str | None] = []

    def audited_runner(client: Any, **kwargs: Any) -> RunResult:
        seen.update(kwargs)
        return audited_result_for("LOCAL_FIX", audit_id=kwargs["audit_id"])

    def audited_client_factory(runfile: str | None) -> FakeClient:
        runfiles.append(runfile)
        return FakeClient()

    code = main(
        audited_args()
        + [
            "--acceptance-criterion",
            "Worker returns the ACK",
            "--constraint",
            "do not modify files",
            "--constraint",
            "do not run Git",
            "--evidence",
            "Auditor returned READY",
            "--audit-id",
            "audit-cli-audited",
            "--poll-interval",
            "0.5",
            "--timeout",
            "30",
            "--runfile",
            "audited-running.json",
        ],
        client_factory=audited_client_factory,
        runner=lambda *_args, **_kwargs: pytest.fail("direct runner called"),
        audited_runner=audited_runner,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert runfiles == ["audited-running.json"]
    assert seen == {
        "auditor_session_id": "auditor-1",
        "planner_session_id": "planner-1",
        "worker_session_id": "worker-1",
        "task_goal": "verify the audited loop",
        "acceptance_criteria": [
            "Auditor returns an AuditReport",
            "Worker returns the ACK",
        ],
        "constraints": ["do not modify files", "do not run Git"],
        "evidence": ["Auditor returned READY"],
        "audit_id": "audit-cli-audited",
        "poll_interval": 0.5,
        "timeout": 30.0,
    }
    assert payload["auditReport"] == {
        "auditId": "audit-cli-audited",
        "targetSessionId": "worker-1",
        "finding": "acceptance evidence is insufficient",
        "evidence": ["Auditor returned READY"],
        "recommendedDecision": "LOCAL_FIX",
    }
    assert payload["auditorTurnId"] == "auditor-turn"
    assert payload["auditorClientMessageId"] == "auditor-message"
    assert payload["plannerTurnId"] == "planner-turn"


@pytest.mark.parametrize(("decision", "expected_code"), [("PASS", 0), ("HUMAN", 2)])
def test_audited_cli_maps_result_exit_codes(
    capsys: pytest.CaptureFixture[str], decision: str, expected_code: int
) -> None:
    code = main(
        audited_args(),
        client_factory=client_factory,
        audited_runner=lambda *_args, **_kwargs: audited_result_for(decision),
    )

    assert code == expected_code
    assert json.loads(capsys.readouterr().out)["decision"] == decision


@pytest.mark.parametrize(
    "args",
    [
        [],
        base_args() + ["--poll-interval", "0"],
        base_args() + ["--timeout", "nan"],
        base_args() + ["--recommended-decision", "UNKNOWN"],
        base_args() + ["--evidence", ""],
        base_args() + ["--audit-id", ""],
    ],
)
def test_cli_argument_errors_are_json_with_exit_one(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    code = main(args, client_factory=client_factory)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert captured.err == ""
    assert payload["error"]["code"] == "usage_error"


@pytest.mark.parametrize(
    "args",
    [
        [
            "--planner-session",
            "planner-1",
            "--worker-session",
            "worker-1",
        ],
        [
            "--planner-session",
            "planner-1",
            "--worker-session",
            "worker-1",
            "--auditor-session",
            "auditor-1",
            "--acceptance-criterion",
            "criterion",
        ],
        [
            "--planner-session",
            "planner-1",
            "--worker-session",
            "worker-1",
            "--auditor-session",
            "auditor-1",
            "--task-goal",
            "goal",
        ],
        audited_args() + ["--finding", "must not be supplied"],
        audited_args() + ["--recommended-decision", "LOCAL_FIX"],
        base_args() + ["--constraint", "audited only"],
    ],
)
def test_mode_combination_errors_fail_before_client_creation(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    client_calls = 0

    def forbidden_client_factory(runfile: str | None) -> FakeClient:
        nonlocal client_calls
        client_calls += 1
        return FakeClient()

    code = main(args, client_factory=forbidden_client_factory)

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"]["code"] == "usage_error"
    assert client_calls == 0


def test_cli_protocol_error_is_json_with_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(client: Any, **kwargs: Any) -> RunResult:
        raise LoopProtocolError("PlannerDecision is invalid")

    code = main(
        base_args(), client_factory=client_factory, runner=runner
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": {
            "code": "protocol_error",
            "message": "PlannerDecision is invalid",
        }
    }


def test_audited_cli_protocol_error_is_json_with_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def audited_runner(client: Any, **kwargs: Any) -> RunResult:
        raise LoopProtocolError("AuditReport is invalid")

    code = main(
        audited_args(),
        client_factory=client_factory,
        audited_runner=audited_runner,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": {
            "code": "protocol_error",
            "message": "AuditReport is invalid",
        }
    }


def test_observed_cli_passes_defaults_and_outputs_single_result_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def observed_runner(client: Any, **kwargs: Any) -> ObservedLoopResult:
        seen.update(kwargs)
        return observed_result_for("PASS", audit_id=kwargs["audit_id"])

    code = main(
        observed_args(),
        client_factory=client_factory,
        runner=lambda *_args, **_kwargs: pytest.fail("direct runner called"),
        audited_runner=lambda *_args, **_kwargs: pytest.fail(
            "one-shot audited runner called"
        ),
        observed_runner=observed_runner,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert payload == {
        "auditId": "root-observed",
        "termination": "PASS",
        "reason": "observed loop returned PASS",
        "rounds": [],
        "auditCount": 0,
    }
    assert seen["observe_interval"] == 2.0
    assert seen["stall_threshold"] == 300.0
    assert seen["failure_threshold"] == 2
    assert seen["max_audits"] == 3
    assert seen["overall_timeout"] == 600.0
    assert seen["poll_interval"] == 2.0
    assert seen["timeout"] == 90.0


def test_observed_cli_passes_explicit_limits_and_maps_human_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def observed_runner(client: Any, **kwargs: Any) -> ObservedLoopResult:
        seen.update(kwargs)
        return observed_result_for("HUMAN", audit_id=kwargs["audit_id"])

    code = main(
        observed_args()
        + [
            "--observe-interval",
            "0.25",
            "--stall-threshold",
            "0",
            "--failure-threshold",
            "3",
            "--max-audits",
            "4",
            "--overall-timeout",
            "30",
            "--poll-interval",
            "0.5",
            "--timeout",
            "5",
        ],
        client_factory=client_factory,
        observed_runner=observed_runner,
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out)["termination"] == "HUMAN"
    assert seen["observe_interval"] == 0.25
    assert seen["stall_threshold"] == 0.0
    assert seen["failure_threshold"] == 3
    assert seen["max_audits"] == 4
    assert seen["overall_timeout"] == 30.0
    assert seen["poll_interval"] == 0.5
    assert seen["timeout"] == 5.0


@pytest.mark.parametrize(
    "args",
    [
        base_args() + ["--observe", "--audit-id", "root"],
        audited_args() + ["--observe"],
        audited_args() + ["--observe-interval", "1"],
        observed_args() + ["--max-audits", "0"],
        observed_args() + ["--failure-threshold", "1.5"],
        observed_args() + ["--stall-threshold", "-1"],
        observed_args() + ["--overall-timeout", "nan"],
    ],
)
def test_invalid_observed_arguments_fail_before_client_creation(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    client_calls = 0

    def forbidden_client_factory(runfile: str | None) -> FakeClient:
        nonlocal client_calls
        client_calls += 1
        return FakeClient()

    code = main(args, client_factory=forbidden_client_factory)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"
    assert client_calls == 0


def test_cli_help_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--help"], client_factory=client_factory)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert payload["help"].startswith("usage: clao")


def test_gate_cli_parses_commands_and_does_not_create_client_on_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client_calls = 0
    seen: dict[str, Any] = {}

    def forbidden_client_factory(runfile: str | None) -> FakeClient:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("passing Gate must not create AOClient")

    def gated_runner(factory: Callable[[], Any], **kwargs: Any) -> GatedRunResult:
        seen["factory"] = factory
        seen.update(kwargs)
        return gated_result_for(passed=True)

    code = main(
        gate_args()
        + [
            "--gate-command-json",
            '["python","-m","pip","check"]',
            "--gate-timeout",
            "15",
            "--gate-output-limit",
            "1234",
        ],
        client_factory=forbidden_client_factory,
        gated_runner=gated_runner,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert client_calls == 0
    assert seen["gate_commands"] == [
        ["python", "-m", "pytest"],
        ["python", "-m", "pip", "check"],
    ]
    assert seen["gate_timeout"] == 15.0
    assert seen["gate_output_limit"] == 1234
    assert payload["gate"]["passed"] is True
    assert payload["gate"]["steps"][0]["duration_seconds"] == 1.25
    assert payload["auditedResult"] is None


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (gated_result_for(passed=False, decision="LOCAL_FIX"), 3),
        (gated_result_for(passed=False, decision="PASS"), 3),
        (gated_result_for(passed=False, decision="HUMAN"), 2),
        (
            gated_result_for(
                passed=False,
                precondition_failure=True,
            ),
            3,
        ),
    ],
)
def test_gate_cli_maps_failed_and_human_exit_codes(
    capsys: pytest.CaptureFixture[str],
    result: GatedRunResult,
    expected_code: int,
) -> None:
    code = main(
        gate_args(),
        client_factory=client_factory,
        gated_runner=lambda *_args, **_kwargs: result,
    )

    assert code == expected_code
    assert json.loads(capsys.readouterr().out)["gate"]["passed"] is False


@pytest.mark.parametrize(
    "args",
    [
        gate_args() + ["--gate-command-json", "not-json"],
        gate_args() + ["--gate-command-json", "{}"],
        gate_args() + ["--gate-command-json", "[]"],
        gate_args() + ["--gate-command-json", '["python",3]'],
        gate_args() + ["--observe"],
        gate_args() + ["--finding", "forbidden"],
        gate_args() + ["--recommended-decision", "LOCAL_FIX"],
        audited_args() + ["--gate-repo", "main"],
        [
            "--planner-session",
            "planner-1",
            "--worker-session",
            "worker-1",
            "--gate",
            "--audit-id",
            "root-gate",
            "--task-goal",
            "goal",
            "--acceptance-criterion",
            "criterion",
            "--gate-repo",
            "main",
            "--gate-command-json",
            '["python","-m","pytest"]',
        ],
    ],
)
def test_invalid_gate_arguments_fail_before_client_and_gate(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    client_calls = 0
    gate_calls = 0

    def forbidden_client_factory(runfile: str | None) -> FakeClient:
        nonlocal client_calls
        client_calls += 1
        return FakeClient()

    def forbidden_gated_runner(*_args: Any, **_kwargs: Any) -> GatedRunResult:
        nonlocal gate_calls
        gate_calls += 1
        return gated_result_for(passed=True)

    code = main(
        args,
        client_factory=forbidden_client_factory,
        gated_runner=forbidden_gated_runner,
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "usage_error"
    )
    assert client_calls == 0
    assert gate_calls == 0


def test_gate_runtime_error_is_json_with_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def gated_runner(*_args: Any, **_kwargs: Any) -> GatedRunResult:
        raise LoopProtocolError("Gate AuditReport is invalid")

    code = main(
        gate_args(),
        client_factory=client_factory,
        gated_runner=gated_runner,
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": {
            "code": "protocol_error",
            "message": "Gate AuditReport is invalid",
        }
    }


def test_cli_writes_utf8_json_when_console_encoding_cannot_encode_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyConsole:
        encoding = "gbk"

        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        def write(self, value: str) -> int:
            return len(value.encode(self.encoding))

    console = LegacyConsole()
    result = gated_result_for(passed=True)
    step = result.gate_result.steps[0]
    result = GatedRunResult(
        gate_result=IntegrationGateResult(
            commit_sha=result.gate_result.commit_sha,
            passed=True,
            steps=(
                GateStepResult(
                    argv=step.argv,
                    exit_code=step.exit_code,
                    timed_out=step.timed_out,
                    duration_seconds=step.duration_seconds,
                    stdout="path contains replacement character: \ufffd",
                    stderr=step.stderr,
                ),
            ),
            failure_reason=None,
        ),
    )
    monkeypatch.setattr(sys, "stdout", console)

    code = main(
        gate_args(),
        client_factory=client_factory,
        gated_runner=lambda *_args, **_kwargs: result,
    )

    assert code == 0
    payload = json.loads(console.buffer.getvalue().decode("utf-8"))
    assert payload["gate"]["steps"][0]["stdout"].endswith("\ufffd")
