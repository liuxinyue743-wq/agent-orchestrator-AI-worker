"""Integration Gate: run the pre-configured gate_commands in the Worker's
actual worktree and record exit code / stdout / stderr. All commands must
exit 0 to reach DONE. Gate failure -> INTEGRATION_FAILED evidence, re-enters
the loop (still bounded by budgets).

Gate NEVER runs commands invented by Auditor/Planner — only
TaskSpec.gate_commands, and NEVER through a shell (argv only): a gate command
is author-controlled configuration, so `>`/`|`/`&&` are not interpreted — a
command that needs a shell simply fails closed (visible in the evidence).

Repository-integrity watchdog: a read-only, content-sensitive Git snapshot is
captured before and after the command batch.  Gate commands may inspect the
Worker's existing dirty state, but may not change HEAD, index, tracked source,
or non-artifact untracked content while producing verification evidence.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import worktree as wt
from .mission_contracts import TaskSpec
from .event_normalizer import now_iso
from .state_store import StateStore


def _to_argv(cmd: str) -> List[str]:
    """Split a gate command into argv WITHOUT a shell.

    posix mode off on Windows (shlex posix=True would eat backslashes in
    paths); surrounding quotes stripped per token. Returns [] when the
    command cannot be parsed (fail-closed: caller records exit -1).

    The head token is resolved through PATH via shutil.which: on Windows,
    CreateProcess searches the PARENT PROCESS image directory before PATH —
    for a venv interpreter that is the BASE runtime's directory, so a bare
    `python` would silently resolve to the base interpreter (no pytest)
    instead of the venv the operator activated. which() honors PATH order.
    """
    import shutil
    try:
        parts = shlex.split(cmd or "", posix=(os.name != "nt"))
    except ValueError:
        return []
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1]
                 and p[0] in ("\"", "'") else p for p in parts]
    parts = [p for p in parts if p]
    if parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


@dataclass
class GateRun:
    ok: bool
    results: List[dict]
    head_before: Optional[str] = None
    head_after: Optional[str] = None
    command_ok: bool = True
    integrity_ok: bool = True
    integrity_error: Optional[str] = None
    initial_clean: Optional[bool] = None
    state_digest_before: Optional[str] = None
    state_digest_after: Optional[str] = None

    @property
    def head_mutated(self) -> bool:
        """True when running the gate MOVED the worktree's HEAD ref."""
        return bool(self.head_before and self.head_after
                    and self.head_before != self.head_after)

    def evidence(self) -> List[dict]:
        ev = [{"type": "integration_gate",
               "summary": ("pass" if self.ok else "fail") +
                          " commands=" + str(len(self.results)),
               "reference": "; ".join(
                   "exit=%s" % r.get("exit_code") for r in self.results)}]
        if not self.integrity_ok:
            ev.append({
                "type": "gate_repository_integrity",
                "summary": self.integrity_error or
                           "Gate repository integrity failed",
                "reference": (
                    "initial_clean=%s; head_before=%s; head_after=%s; "
                    "state_digest_before=%s; state_digest_after=%s" %
                    (self.initial_clean, self.head_before, self.head_after,
                     self.state_digest_before, self.state_digest_after)),
            })
        if self.head_mutated:
            ev.append({"type": "gate_head_mutation",
                       "summary": "gate execution moved HEAD %s -> %s"
                                  % (self.head_before, self.head_after),
                       "reference": "git rev-parse HEAD before/after"})
        return ev


class IntegrationGate:
    def __init__(self, store: StateStore):
        self.store = store

    def run(self, task: TaskSpec, worktree_path: str, *,
            require_clean: bool = False) -> GateRun:
        results = []
        cwd = Path(worktree_path)
        try:
            before = wt.git_state_snapshot(str(cwd))
        except wt.GitStateSnapshotError as exc:
            return GateRun(
                ok=False, results=results, command_ok=False,
                integrity_ok=False,
                integrity_error=("pre-Gate Git probe failed: %s" %
                                 str(exc)[:1000]))
        if require_clean and not before.clean:
            return GateRun(
                ok=False, results=results, head_before=before.head,
                command_ok=False, integrity_ok=False,
                integrity_error="initial repository not clean",
                initial_clean=False, state_digest_before=before.digest)

        command_ok = True
        for cmd in task.gate_commands:
            started = now_iso()
            argv = _to_argv(cmd)
            if not argv:
                exit_code, stdout, stderr = -1, "", \
                    "gate command unparseable as argv (shell is disabled)"
            else:
                try:
                    proc = subprocess.run(argv, shell=False, cwd=str(cwd),
                                          capture_output=True, text=True,
                                          timeout=300, encoding="utf-8",
                                          errors="replace")
                    exit_code = proc.returncode
                    stdout = (proc.stdout or "")[:8000]
                    stderr = (proc.stderr or "")[:8000]
                except Exception as e:
                    exit_code, stdout, stderr = -1, "", str(e)
            ended = now_iso()
            results.append({"command": cmd, "argv": argv, "cwd": str(cwd),
                            "exit_code": exit_code, "stdout": stdout,
                            "stderr": stderr, "started_at": started,
                            "ended_at": ended})
            self.store.record_gate_run(task_id=task.task_id, command=cmd,
                cwd=str(cwd), exit_code=exit_code, started_at=started,
                ended_at=ended, stdout=stdout, stderr=stderr)
            if exit_code != 0:
                command_ok = False

        try:
            after = wt.git_state_snapshot(str(cwd))
        except wt.GitStateSnapshotError as exc:
            return GateRun(
                ok=False, results=results, head_before=before.head,
                command_ok=command_ok, integrity_ok=False,
                integrity_error=("post-Gate Git probe failed: %s" %
                                 str(exc)[:1000]),
                initial_clean=before.clean,
                state_digest_before=before.digest)

        integrity_error = None
        if before.head != after.head:
            integrity_error = "Gate changed HEAD"
        elif before.digest != after.digest:
            integrity_error = "Gate changed repository state"
        integrity_ok = integrity_error is None
        return GateRun(
            ok=command_ok and integrity_ok,
            results=results,
            head_before=before.head,
            head_after=after.head,
            command_ok=command_ok,
            integrity_ok=integrity_ok,
            integrity_error=integrity_error,
            initial_clean=before.clean,
            state_digest_before=before.digest,
            state_digest_after=after.digest,
        )
