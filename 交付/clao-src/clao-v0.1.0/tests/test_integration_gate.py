from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from closed_loop_agent_orchestrator import (
    GateStepResult,
    IntegrationGateResult,
    run_integration_gate,
)


COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
CHANGED_COMMIT_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def completed(
    argv: Sequence[str],
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        tuple(argv), exit_code, stdout=stdout, stderr=stderr
    )


class FakeRunner:
    def __init__(
        self,
        outcomes: Sequence[
            subprocess.CompletedProcess[str]
            | OSError
            | subprocess.TimeoutExpired
        ],
    ) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(
        self, argv: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), kwargs))
        if not self.outcomes:
            raise AssertionError(f"unexpected subprocess call: {argv!r}")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def clean_git_outcomes() -> list[subprocess.CompletedProcess[str]]:
    return [
        completed(("git", "rev-parse", "HEAD"), stdout=COMMIT_SHA + "\n"),
        completed(("git", "status", "--porcelain")),
    ]


def assert_safe_invocations(runner: FakeRunner, root: Path) -> None:
    for _, kwargs in runner.calls:
        assert kwargs["cwd"] == root.resolve()
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["shell"] is False
        assert kwargs["check"] is False


def test_clean_git_checkout_passes_and_records_immutable_facts(
    tmp_path: Path,
) -> None:
    command = ("python", "-m", "pytest")
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(command, stdout="3 passed\n"),
            *clean_git_outcomes(),
        ]
    )
    ticks = iter(
        (1.0, 1.1, 2.0, 2.1, 3.0, 4.25, 5.0, 5.1, 6.0, 6.1)
    )

    result = run_integration_gate(
        tmp_path,
        [command],
        runner=runner,
        monotonic=lambda: next(ticks),
    )

    assert result == IntegrationGateResult(
        commit_sha=COMMIT_SHA,
        passed=True,
        steps=(
            GateStepResult(
                argv=command,
                exit_code=0,
                timed_out=False,
                duration_seconds=1.25,
                stdout="3 passed\n",
                stderr="",
            ),
        ),
        failure_reason=None,
    )
    assert result.to_dict() == {
        "commit_sha": COMMIT_SHA,
        "passed": True,
        "steps": [
            {
                "argv": ["python", "-m", "pytest"],
                "exit_code": 0,
                "timed_out": False,
                "duration_seconds": 1.25,
                "stdout": "3 passed\n",
                "stderr": "",
            }
        ],
        "failure_reason": None,
    }
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.steps[0].stdout = "changed"  # type: ignore[misc]
    assert_safe_invocations(runner, tmp_path)


def test_non_git_directory_fails_before_status_or_gate_command(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            completed(
                ("git", "rev-parse", "HEAD"),
                exit_code=128,
                stderr="fatal: not a git repository\n",
            )
        ]
    )

    result = run_integration_gate(tmp_path, [["gate"]], runner=runner)

    assert result.passed is False
    assert result.commit_sha is None
    assert result.steps == ()
    assert "git rev-parse HEAD exited with code 128" in result.failure_reason
    assert [call[0] for call in runner.calls] == [
        ("git", "rev-parse", "HEAD")
    ]


def test_dirty_worktree_fails_before_gate_command(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            clean_git_outcomes()[0],
            completed(
                ("git", "status", "--porcelain"),
                stdout=" M src/example.py\n",
            ),
        ]
    )

    result = run_integration_gate(tmp_path, [["gate"]], runner=runner)

    assert result.passed is False
    assert result.commit_sha == COMMIT_SHA
    assert result.steps == ()
    assert result.failure_reason == (
        "Git working tree is not clean:  M src/example.py"
    )
    assert [call[0] for call in runner.calls] == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
    ]


def test_multiple_commands_run_in_order_and_all_succeed(tmp_path: Path) -> None:
    commands = (("build", "--offline"), ("test",), ("package", "--check"))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0]),
            *clean_git_outcomes(),
            completed(commands[1]),
            *clean_git_outcomes(),
            completed(commands[2]),
            *clean_git_outcomes(),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    assert result.passed is True
    assert tuple(step.argv for step in result.steps) == commands
    assert [call[0] for call in runner.calls] == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
        commands[0],
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
        commands[1],
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
        commands[2],
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
    ]


def test_successful_command_that_changes_head_fails_before_next_command(
    tmp_path: Path,
) -> None:
    commands = (("commit-changes",), ("must-not-run",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0]),
            completed(
                ("git", "rev-parse", "HEAD"),
                stdout=CHANGED_COMMIT_SHA + "\n",
            ),
            completed(("git", "status", "--porcelain")),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    assert result.passed is False
    assert tuple(step.argv for step in result.steps) == (commands[0],)
    assert result.failure_reason == (
        "Gate command 1 changed HEAD: expected commit "
        f"{COMMIT_SHA}, actual commit {CHANGED_COMMIT_SHA}"
    )
    assert [call[0] for call in runner.calls][-2:] == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
    ]


def test_successful_command_that_dirties_worktree_fails_before_next_command(
    tmp_path: Path,
) -> None:
    commands = (("write-artifact",), ("must-not-run",))
    porcelain = " M generated.txt\n?? artifact.txt\n"
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0]),
            completed(
                ("git", "rev-parse", "HEAD"), stdout=COMMIT_SHA + "\n"
            ),
            completed(
                ("git", "status", "--porcelain"), stdout=porcelain
            ),
        ]
    )

    result = run_integration_gate(
        tmp_path,
        commands,
        output_limit_chars=8,
        runner=runner,
    )

    assert result.passed is False
    assert tuple(step.argv for step in result.steps) == (commands[0],)
    assert result.failure_reason == (
        "Gate command 1 changed the working tree: "
        + porcelain.rstrip("\r\n")[:8]
    )
    assert commands[1] not in [call[0] for call in runner.calls]


def test_post_command_rev_parse_failure_stops_gate(tmp_path: Path) -> None:
    commands = (("first",), ("must-not-run",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0]),
            completed(
                ("git", "rev-parse", "HEAD"),
                exit_code=128,
                stderr="cannot read HEAD",
            ),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    assert result.passed is False
    assert tuple(step.argv for step in result.steps) == (commands[0],)
    assert result.failure_reason == (
        "after command 1: git rev-parse HEAD exited with code 128: "
        "cannot read HEAD"
    )
    assert commands[1] not in [call[0] for call in runner.calls]


def test_post_command_git_status_failure_stops_gate(tmp_path: Path) -> None:
    commands = (("first",), ("must-not-run",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0]),
            completed(
                ("git", "rev-parse", "HEAD"), stdout=COMMIT_SHA + "\n"
            ),
            completed(
                ("git", "status", "--porcelain"),
                exit_code=128,
                stderr="status failed",
            ),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    assert result.passed is False
    assert tuple(step.argv for step in result.steps) == (commands[0],)
    assert result.failure_reason == (
        "after command 1: git status --porcelain exited with code 128: "
        "status failed"
    )
    assert commands[1] not in [call[0] for call in runner.calls]


def test_first_nonzero_command_stops_remaining_commands(tmp_path: Path) -> None:
    commands = (("first",), ("must-not-run",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0], exit_code=7, stderr="failed\n"),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    assert result.passed is False
    assert len(result.steps) == 1
    assert result.steps[0].exit_code == 7
    assert result.failure_reason == (
        'command 1 exited with code 7: ["first"]'
    )
    assert [call[0] for call in runner.calls][-1] == commands[0]


def test_missing_command_is_a_failed_step_and_stops(tmp_path: Path) -> None:
    commands = (("missing-tool",), ("must-not-run",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            FileNotFoundError(2, "The system cannot find the file"),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)

    step = result.steps[0]
    assert result.passed is False
    assert step.argv == commands[0]
    assert step.exit_code is None
    assert step.timed_out is False
    assert "FileNotFoundError" in step.stderr
    assert "could not start" in result.failure_reason
    assert len(runner.calls) == 3


def test_timeout_is_a_failed_step_with_partial_output(tmp_path: Path) -> None:
    command = ("slow-test",)
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            subprocess.TimeoutExpired(
                command,
                timeout=2.5,
                output=b"partial stdout",
                stderr=b"partial stderr",
            ),
        ]
    )

    result = run_integration_gate(
        tmp_path,
        [command],
        timeout_seconds=2.5,
        runner=runner,
    )

    step = result.steps[0]
    assert result.passed is False
    assert step.exit_code is None
    assert step.timed_out is True
    assert step.stdout == "partial stdout"
    assert step.stderr == "partial stderr"
    assert result.failure_reason == (
        'command 1 timed out after 2.5 seconds: ["slow-test"]'
    )


def test_stdout_and_stderr_are_prefix_truncated_to_configured_limit(
    tmp_path: Path,
) -> None:
    command = ("noisy",)
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(command, stdout="abcdefgh", stderr="uvwxyz"),
            *clean_git_outcomes(),
        ]
    )

    result = run_integration_gate(
        tmp_path,
        [command],
        output_limit_chars=4,
        runner=runner,
    )

    assert result.passed is True
    assert result.steps[0].stdout == "abcd"
    assert result.steps[0].stderr == "uvwx"


@pytest.mark.parametrize(
    "commands",
    [
        "python -m pytest",
        ["python -m pytest"],
        [[]],
        [],
    ],
)
def test_shell_strings_and_empty_argv_are_rejected_without_subprocess_calls(
    tmp_path: Path,
    commands: Any,
) -> None:
    runner = FakeRunner([])

    with pytest.raises((TypeError, ValueError)):
        run_integration_gate(tmp_path, commands, runner=runner)

    assert runner.calls == []


def test_failure_evidence_contains_argv_exit_code_and_only_facts(
    tmp_path: Path,
) -> None:
    command = ("contract-test", "--case", "integration")
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(command, exit_code=9, stderr="contract mismatch"),
        ]
    )

    result = run_integration_gate(tmp_path, [command], runner=runner)
    evidence = result.to_evidence()

    assert (
        'integration_gate.step[1].argv=["contract-test","--case","integration"]'
        in evidence
    )
    assert "integration_gate.step[1].exit_code=9" in evidence
    assert "integration_gate.step[1].timed_out=false" in evidence
    assert f"integration_gate.commit_sha={COMMIT_SHA}" in evidence
    assert all(
        decision not in evidence
        for decision in ("PASS", "LOCAL_FIX", "REPLAN", "HUMAN")
    )


def test_audit_evidence_omits_duration_and_successful_step_output(
    tmp_path: Path,
) -> None:
    commands = (("build",), ("contract-test",))
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            completed(commands[0], stdout="successful build output"),
            *clean_git_outcomes(),
            completed(
                commands[1],
                exit_code=7,
                stdout="bounded failure output",
                stderr="bounded failure error",
            ),
        ]
    )

    result = run_integration_gate(tmp_path, commands, runner=runner)
    evidence = result.to_evidence()

    assert "duration_seconds" not in evidence
    assert "successful build output" not in evidence
    assert "integration_gate.step[1].stdout" not in evidence
    assert "integration_gate.step[1].stderr" not in evidence
    assert "integration_gate.step[2].stdout=bounded failure output" in evidence
    assert "integration_gate.step[2].stderr=bounded failure error" in evidence


def test_audit_evidence_is_stable_when_only_duration_changes() -> None:
    common = {
        "argv": ("contract-test",),
        "exit_code": 7,
        "timed_out": False,
        "stdout": "failure output",
        "stderr": "failure error",
    }
    first = IntegrationGateResult(
        commit_sha=COMMIT_SHA,
        passed=False,
        steps=(GateStepResult(duration_seconds=0.25, **common),),
        failure_reason='command 1 exited with code 7: ["contract-test"]',
    )
    second = IntegrationGateResult(
        commit_sha=COMMIT_SHA,
        passed=False,
        steps=(GateStepResult(duration_seconds=19.75, **common),),
        failure_reason=first.failure_reason,
    )

    assert first.to_evidence() == second.to_evidence()


def test_timeout_evidence_identifies_command_and_timeout(tmp_path: Path) -> None:
    command = ("slow", "--forever")
    runner = FakeRunner(
        [
            *clean_git_outcomes(),
            subprocess.TimeoutExpired(command, timeout=1),
        ]
    )

    result = run_integration_gate(
        tmp_path, [command], timeout_seconds=1, runner=runner
    )
    evidence = result.to_evidence()

    assert 'integration_gate.step[1].argv=["slow","--forever"]' in evidence
    assert "timed out after 1 seconds" in evidence
    assert "integration_gate.step[1].timed_out=true" in evidence
