"""Command-line entry point for one closed-loop AO run."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .ao_client import AOClient, AOClientError
from .loop_runner import (
    DECISIONS,
    GatedRunResult,
    LoopRunError,
    ObservedLoopResult,
    RunResult,
    run_audited_once,
    run_gated_once,
    run_observed_loop,
    run_once,
)


class _CLIUsageError(ValueError):
    pass


class _CLIHelp(RuntimeError):
    pass


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CLIUsageError(message)

    def print_help(self, file: Any = None) -> None:
        raise _CLIHelp(self.format_help())


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(prog="clao")
    parser.add_argument("--planner-session", required=True)
    parser.add_argument("--worker-session", required=True)
    parser.add_argument("--auditor-session")
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--gate-repo")
    parser.add_argument(
        "--gate-command-json", action="append", type=_json_argv
    )
    parser.add_argument("--gate-timeout", type=_positive_float)
    parser.add_argument("--gate-output-limit", type=_non_negative_int)
    parser.add_argument("--task-goal")
    parser.add_argument("--acceptance-criterion", action="append", default=[])
    parser.add_argument("--constraint", action="append", default=[])
    parser.add_argument("--finding")
    parser.add_argument("--audit-id")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--recommended-decision", choices=sorted(DECISIONS))
    parser.add_argument("--poll-interval", type=_positive_float, default=2.0)
    parser.add_argument("--timeout", type=_positive_float, default=90.0)
    parser.add_argument("--observe-interval", type=_positive_float)
    parser.add_argument("--stall-threshold", type=_non_negative_float)
    parser.add_argument("--failure-threshold", type=_positive_int)
    parser.add_argument("--max-audits", type=_positive_int)
    parser.add_argument("--overall-timeout", type=_positive_float)
    parser.add_argument("--runfile")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., AOClient] = AOClient,
    runner: Callable[..., RunResult] = run_once,
    audited_runner: Callable[..., RunResult] = run_audited_once,
    observed_runner: Callable[..., ObservedLoopResult] = run_observed_loop,
    gated_runner: Callable[..., GatedRunResult] = run_gated_once,
) -> int:
    """Parse CLI arguments, write exactly one JSON object, and return an exit code."""

    try:
        args = build_parser().parse_args(argv)
        _validate_text_arguments(args)
        if args.gate:
            result = gated_runner(
                lambda: client_factory(args.runfile),
                auditor_session_id=args.auditor_session,
                planner_session_id=args.planner_session,
                worker_session_id=args.worker_session,
                task_goal=args.task_goal,
                acceptance_criteria=args.acceptance_criterion,
                constraints=args.constraint,
                evidence=args.evidence,
                audit_id=args.audit_id,
                gate_repo=args.gate_repo,
                gate_commands=args.gate_command_json,
                gate_timeout=args.gate_timeout or 300.0,
                gate_output_limit=(
                    20_000
                    if args.gate_output_limit is None
                    else args.gate_output_limit
                ),
                poll_interval=args.poll_interval,
                timeout=args.timeout,
            )
        else:
            with client_factory(args.runfile) as client:
                result = _run_non_gate_mode(
                    args,
                    client,
                    runner=runner,
                    audited_runner=audited_runner,
                    observed_runner=observed_runner,
                )
    except _CLIHelp as exc:
        _write_json({"help": str(exc)})
        return 0
    except _CLIUsageError as exc:
        _write_error("usage_error", str(exc))
        return 1
    except LoopRunError as exc:
        _write_error(exc.code, str(exc))
        return 1
    except AOClientError as exc:
        _write_error("ao_error", str(exc))
        return 1
    except ValueError as exc:
        _write_error("usage_error", str(exc))
        return 1
    except Exception as exc:  # pragma: no cover - final JSON-only CLI boundary
        _write_error(
            "runtime_error", f"unexpected runtime failure ({type(exc).__name__})"
        )
        return 1

    _write_json(result.as_dict())
    if isinstance(result, GatedRunResult):
        if result.gate_result.passed:
            return 0
        if (
            result.audited_result is not None
            and result.audited_result.decision.decision.value == "HUMAN"
        ):
            return 2
        return 3
    if isinstance(result, ObservedLoopResult):
        return 2 if result.termination.value == "HUMAN" else 0
    return 2 if result.decision.decision.value == "HUMAN" else 0


def _run_non_gate_mode(
    args: argparse.Namespace,
    client: AOClient,
    *,
    runner: Callable[..., RunResult],
    audited_runner: Callable[..., RunResult],
    observed_runner: Callable[..., ObservedLoopResult],
) -> RunResult | ObservedLoopResult:
    if args.observe:
        return observed_runner(
            client,
            auditor_session_id=args.auditor_session,
            planner_session_id=args.planner_session,
            worker_session_id=args.worker_session,
            task_goal=args.task_goal,
            acceptance_criteria=args.acceptance_criterion,
            constraints=args.constraint,
            evidence=args.evidence,
            audit_id=args.audit_id,
            observe_interval=args.observe_interval or 2.0,
            stall_threshold=(
                300.0
                if args.stall_threshold is None
                else args.stall_threshold
            ),
            failure_threshold=args.failure_threshold or 2,
            max_audits=args.max_audits or 3,
            overall_timeout=args.overall_timeout or 600.0,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    if args.auditor_session is None:
        return runner(
            client,
            planner_session_id=args.planner_session,
            worker_session_id=args.worker_session,
            finding=args.finding,
            audit_id=args.audit_id,
            evidence=args.evidence,
            recommended_decision=args.recommended_decision,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    return audited_runner(
        client,
        auditor_session_id=args.auditor_session,
        planner_session_id=args.planner_session,
        worker_session_id=args.worker_session,
        task_goal=args.task_goal,
        acceptance_criteria=args.acceptance_criterion,
        constraints=args.constraint,
        evidence=args.evidence,
        audit_id=args.audit_id,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _json_argv(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("must be a JSON array")
    if not parsed:
        raise argparse.ArgumentTypeError("must not be an empty JSON array")
    if not all(isinstance(item, str) for item in parsed):
        raise argparse.ArgumentTypeError("must contain only strings")
    if not parsed[0].strip():
        raise argparse.ArgumentTypeError(
            "executable must be a non-blank string"
        )
    return parsed


def _validate_text_arguments(args: argparse.Namespace) -> None:
    for name in ("planner_session", "worker_session"):
        value = getattr(args, name)
        if not value.strip():
            raise _CLIUsageError(f"--{name.replace('_', '-')} must not be empty")
    if any(not item.strip() for item in args.evidence):
        raise _CLIUsageError("--evidence must not be empty")
    if args.audit_id is not None and not args.audit_id.strip():
        raise _CLIUsageError("--audit-id must not be empty")
    observe_tuning = (
        args.observe_interval,
        args.stall_threshold,
        args.failure_threshold,
        args.max_audits,
        args.overall_timeout,
    )
    gate_options = (
        args.gate_repo,
        args.gate_command_json,
        args.gate_timeout,
        args.gate_output_limit,
    )
    if not args.gate and any(value is not None for value in gate_options):
        raise _CLIUsageError("Gate options require --gate")
    if args.gate:
        if args.observe:
            raise _CLIUsageError("gate mode does not allow --observe")
        if any(value is not None for value in observe_tuning):
            raise _CLIUsageError(
                "gate mode does not allow Observer timing or threshold options"
            )
        if args.finding is not None:
            raise _CLIUsageError("gate mode does not allow --finding")
        if args.recommended_decision is not None:
            raise _CLIUsageError(
                "gate mode does not allow --recommended-decision"
            )
        if args.audit_id is None:
            raise _CLIUsageError("gate mode requires an explicit --audit-id")
        if args.gate_repo is None or not args.gate_repo.strip():
            raise _CLIUsageError("gate mode requires a non-empty --gate-repo")
        if not args.gate_command_json:
            raise _CLIUsageError(
                "gate mode requires at least one --gate-command-json"
            )
        _validate_audited_arguments(args, mode="gate")
        return
    if not args.observe and any(value is not None for value in observe_tuning):
        raise _CLIUsageError(
            "Observer timing and threshold options require --observe"
        )
    if args.observe and args.auditor_session is None:
        raise _CLIUsageError("observed mode requires --auditor-session")
    if args.observe and args.audit_id is None:
        raise _CLIUsageError("observed mode requires an explicit --audit-id")

    if args.auditor_session is None:
        if args.finding is None:
            raise _CLIUsageError("direct mode requires --finding")
        if not args.finding.strip():
            raise _CLIUsageError("--finding must not be empty")
        if (
            args.task_goal is not None
            or args.acceptance_criterion
            or args.constraint
        ):
            raise _CLIUsageError(
                "--task-goal, --acceptance-criterion, and --constraint require "
                "--auditor-session"
            )
        return

    _validate_audited_arguments(
        args,
        mode="observed" if args.observe else "audited",
    )


def _validate_audited_arguments(
    args: argparse.Namespace, *, mode: str
) -> None:
    if args.auditor_session is None:
        raise _CLIUsageError(f"{mode} mode requires --auditor-session")
    if not args.auditor_session.strip():
        raise _CLIUsageError("--auditor-session must not be empty")
    if args.task_goal is None:
        raise _CLIUsageError(f"{mode} mode requires --task-goal")
    if not args.task_goal.strip():
        raise _CLIUsageError("--task-goal must not be empty")
    if not args.acceptance_criterion:
        raise _CLIUsageError(
            f"{mode} mode requires at least one --acceptance-criterion"
        )
    if any(not item.strip() for item in args.acceptance_criterion):
        raise _CLIUsageError("--acceptance-criterion must not be empty")
    if any(not item.strip() for item in args.constraint):
        raise _CLIUsageError("--constraint must not be empty")
    if args.finding is not None:
        raise _CLIUsageError("audited mode does not allow --finding")
    if args.recommended_decision is not None:
        raise _CLIUsageError(
            "audited mode does not allow --recommended-decision"
        )


def _write_error(code: str, message: str) -> None:
    _write_json({"error": {"code": code, "message": message}})


def _write_json(payload: dict[str, Any]) -> None:
    document = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(document)
        return
    buffer.write(document.encode("utf-8"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
