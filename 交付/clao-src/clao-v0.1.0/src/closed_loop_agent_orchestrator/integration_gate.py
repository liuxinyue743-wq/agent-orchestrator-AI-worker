"""Deterministic, offline Integration Gate command execution."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GateStepResult:
    """Immutable facts captured for one configured Gate command."""

    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of this step."""

        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class IntegrationGateResult:
    """Immutable Integration Gate facts without an audit decision."""

    commit_sha: str | None
    passed: bool
    steps: tuple[GateStepResult, ...]
    failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of this Gate run."""

        return {
            "commit_sha": self.commit_sha,
            "passed": self.passed,
            "steps": [step.to_dict() for step in self.steps],
            "failure_reason": self.failure_reason,
        }

    def to_evidence(self) -> str:
        """Render stable facts for a later ``AuditRequest`` evidence item.

        Runtime durations and successful-command output remain available through
        :meth:`to_dict`, but are omitted here so the same Gate failure produces
        the same AO message body and deterministic audit id.
        """

        lines = [
            f"integration_gate.commit_sha={self.commit_sha or 'unavailable'}",
            f"integration_gate.passed={str(self.passed).lower()}",
        ]
        if self.failure_reason is not None:
            lines.append(
                "integration_gate.failure_reason=" + self.failure_reason
            )
        for index, step in enumerate(self.steps, start=1):
            prefix = f"integration_gate.step[{index}]"
            lines.extend(
                (
                    prefix
                    + ".argv="
                    + json.dumps(
                        list(step.argv),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    f"{prefix}.exit_code="
                    + (
                        "unavailable"
                        if step.exit_code is None
                        else str(step.exit_code)
                    ),
                    f"{prefix}.timed_out={str(step.timed_out).lower()}",
                )
            )
            if (
                not self.passed
                and index == len(self.steps)
                and (
                    step.timed_out
                    or step.exit_code is None
                    or step.exit_code != 0
                )
            ):
                lines.extend(
                    (
                        f"{prefix}.stdout={step.stdout}",
                        f"{prefix}.stderr={step.stderr}",
                    )
                )
        return "\n".join(lines)


def run_integration_gate(
    repo_root: str | os.PathLike[str],
    commands: Sequence[Sequence[str]],
    *,
    timeout_seconds: float = 300.0,
    output_limit_chars: int = 20_000,
    runner: SubprocessRunner = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
) -> IntegrationGateResult:
    """Run explicit argv commands sequentially in a clean Git checkout.

    Input/configuration errors raise ``TypeError`` or ``ValueError``. Repository
    checks and command outcomes are returned as deterministic Gate facts.
    """

    normalized_commands = _normalize_commands(commands)
    _validate_timeout(timeout_seconds)
    _validate_output_limit(output_limit_chars)
    root = _normalize_repo_root(repo_root)

    if not root.is_dir():
        return _failure(None, (), f"repo_root is not a directory: {root}")

    commit_sha, porcelain, git_failure = _read_git_state(
        root,
        timeout_seconds,
        runner,
        monotonic,
    )
    if git_failure is not None:
        return _failure(commit_sha, (), git_failure)
    if commit_sha is None or porcelain is None:
        return _failure(
            commit_sha,
            (),
            "Git state probe returned incomplete results",
        )
    if porcelain:
        detail = _truncate(porcelain.rstrip("\r\n"), output_limit_chars)
        reason = "Git working tree is not clean"
        if detail:
            reason += f": {detail}"
        return _failure(commit_sha, (), reason)

    steps: list[GateStepResult] = []
    for index, argv in enumerate(normalized_commands, start=1):
        step = _run_process(
            argv,
            root,
            timeout_seconds,
            output_limit_chars,
            runner,
            monotonic,
        )
        steps.append(step)
        command_json = json.dumps(
            list(argv), ensure_ascii=False, separators=(",", ":")
        )
        if step.timed_out:
            return _failure(
                commit_sha,
                steps,
                f"command {index} timed out after {timeout_seconds:g} "
                f"seconds: {command_json}",
            )
        if step.exit_code is None:
            detail = f": {step.stderr}" if step.stderr else ""
            return _failure(
                commit_sha,
                steps,
                f"command {index} could not start: {command_json}{detail}",
            )
        if step.exit_code != 0:
            return _failure(
                commit_sha,
                steps,
                f"command {index} exited with code {step.exit_code}: "
                f"{command_json}",
            )

        post_commit, post_porcelain, post_failure = _read_git_state(
            root,
            timeout_seconds,
            runner,
            monotonic,
        )
        if post_commit is not None and post_commit != commit_sha:
            return _failure(
                commit_sha,
                steps,
                f"Gate command {index} changed HEAD: expected commit "
                f"{commit_sha}, actual commit {post_commit}",
            )
        if post_failure is not None:
            return _failure(
                commit_sha,
                steps,
                f"after command {index}: {post_failure}",
            )
        if post_commit is None or post_porcelain is None:
            return _failure(
                commit_sha,
                steps,
                f"after command {index}: Git state probe returned "
                "incomplete results",
            )
        if post_porcelain:
            detail = _truncate(
                post_porcelain.rstrip("\r\n"), output_limit_chars
            )
            reason = f"Gate command {index} changed the working tree"
            if detail:
                reason += f": {detail}"
            return _failure(commit_sha, steps, reason)

    return IntegrationGateResult(
        commit_sha=commit_sha,
        passed=True,
        steps=tuple(steps),
        failure_reason=None,
    )


def _read_git_state(
    repo_root: Path,
    timeout_seconds: float,
    runner: SubprocessRunner,
    monotonic: Callable[[], float],
) -> tuple[str | None, str | None, str | None]:
    head = _run_process(
        ("git", "rev-parse", "HEAD"),
        repo_root,
        timeout_seconds,
        None,
        runner,
        monotonic,
    )
    head_failure = _probe_failure("git rev-parse HEAD", head, timeout_seconds)
    if head_failure is not None:
        return None, None, head_failure

    commit_sha = head.stdout.strip()
    if not commit_sha or "\n" in commit_sha or "\r" in commit_sha:
        return (
            None,
            None,
            "git rev-parse HEAD did not return exactly one commit SHA",
        )

    status = _run_process(
        ("git", "status", "--porcelain"),
        repo_root,
        timeout_seconds,
        None,
        runner,
        monotonic,
    )
    status_failure = _probe_failure(
        "git status --porcelain", status, timeout_seconds
    )
    if status_failure is not None:
        return commit_sha, None, status_failure
    return commit_sha, status.stdout, None


def _normalize_commands(
    commands: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
        raise TypeError("commands must be a sequence of argv sequences")
    if not commands:
        raise ValueError("commands must contain at least one argv sequence")

    normalized: list[tuple[str, ...]] = []
    for index, command in enumerate(commands, start=1):
        if isinstance(command, (str, bytes)) or not isinstance(
            command, Sequence
        ):
            raise TypeError(f"command {index} must be an argv sequence")
        argv = tuple(command)
        if not argv:
            raise ValueError(f"command {index} argv must not be empty")
        if not all(isinstance(argument, str) for argument in argv):
            raise TypeError(f"command {index} argv must contain only strings")
        if not argv[0].strip():
            raise ValueError(
                f"command {index} executable must be a non-blank string"
            )
        normalized.append(argv)
    return tuple(normalized)


def _normalize_repo_root(repo_root: str | os.PathLike[str]) -> Path:
    if not isinstance(repo_root, (str, os.PathLike)):
        raise TypeError("repo_root must be a path")
    return Path(repo_root).resolve()


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")


def _validate_output_limit(output_limit_chars: int) -> None:
    if (
        isinstance(output_limit_chars, bool)
        or not isinstance(output_limit_chars, int)
        or output_limit_chars < 0
    ):
        raise ValueError("output_limit_chars must be a non-negative integer")


def _run_process(
    argv: tuple[str, ...],
    repo_root: Path,
    timeout_seconds: float,
    output_limit_chars: int | None,
    runner: SubprocessRunner,
    monotonic: Callable[[], float],
) -> GateStepResult:
    started_at = monotonic()
    try:
        completed = runner(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_seconds = max(0.0, monotonic() - started_at)
        return GateStepResult(
            argv=argv,
            exit_code=None,
            timed_out=True,
            duration_seconds=duration_seconds,
            stdout=_bounded_output(exc.stdout, output_limit_chars),
            stderr=_bounded_output(exc.stderr, output_limit_chars),
        )
    except OSError as exc:
        duration_seconds = max(0.0, monotonic() - started_at)
        return GateStepResult(
            argv=argv,
            exit_code=None,
            timed_out=False,
            duration_seconds=duration_seconds,
            stdout="",
            stderr=_bounded_output(
                f"{type(exc).__name__}: {exc}", output_limit_chars
            ),
        )

    duration_seconds = max(0.0, monotonic() - started_at)
    return GateStepResult(
        argv=argv,
        exit_code=completed.returncode,
        timed_out=False,
        duration_seconds=duration_seconds,
        stdout=_bounded_output(completed.stdout, output_limit_chars),
        stderr=_bounded_output(completed.stderr, output_limit_chars),
    )


def _bounded_output(value: object, limit: int | None) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text if limit is None else _truncate(text, limit)


def _truncate(value: str, limit: int) -> str:
    return value[:limit]


def _probe_failure(
    name: str,
    result: GateStepResult,
    timeout_seconds: float,
) -> str | None:
    if result.timed_out:
        return f"{name} timed out after {timeout_seconds:g} seconds"
    if result.exit_code is None:
        detail = f": {result.stderr}" if result.stderr else ""
        return f"{name} could not start{detail}"
    if result.exit_code != 0:
        output = result.stderr.strip() or result.stdout.strip()
        detail = f": {output}" if output else ""
        return f"{name} exited with code {result.exit_code}{detail}"
    return None


def _failure(
    commit_sha: str | None,
    steps: Sequence[GateStepResult],
    reason: str,
) -> IntegrationGateResult:
    return IntegrationGateResult(
        commit_sha=commit_sha,
        passed=False,
        steps=tuple(steps),
        failure_reason=reason,
    )
