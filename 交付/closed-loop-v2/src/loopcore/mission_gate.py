"""Integration Gate: run the pre-configured gate_commands in the Worker's
actual worktree and record exit code / stdout / stderr. All commands must
exit 0 to reach DONE. Gate failure -> INTEGRATION_FAILED evidence, re-enters
the loop (still bounded by budgets).

Gate NEVER runs commands invented by Auditor/Planner — only
TaskSpec.gate_commands, and NEVER through a shell (argv only): a gate command
is author-controlled configuration, so `>`/`|`/`&&` are not interpreted — a
command that needs a shell simply fails closed (visible in the evidence).

HEAD-mutation watchdog: the gate captures `git rev-parse HEAD` before and
after the command batch. A gate whose execution MOVES the ref (a test that
commits, a hook that rewrites) invalidates 'diff vs frozen base' evidence;
the mutation is surfaced as a deterministic finding for the Verifier.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

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


def _head(repo: Path) -> Optional[str]:
    """Current HEAD sha, or None when unavailable (fail-closed upstream)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


@dataclass
class GateRun:
    ok: bool
    results: List[dict]
    head_before: Optional[str] = None
    head_after: Optional[str] = None

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
                   "exit=%d" % r["exit_code"] for r in self.results)}]
        if self.head_mutated:
            ev.append({"type": "gate_head_mutation",
                       "summary": "gate execution moved HEAD %s -> %s"
                                  % (self.head_before, self.head_after),
                       "reference": "git rev-parse HEAD before/after"})
        return ev


class IntegrationGate:
    def __init__(self, store: StateStore):
        self.store = store

    def run(self, task: TaskSpec, worktree_path: str) -> GateRun:
        results = []
        ok = True
        cwd = Path(worktree_path)
        head_before = _head(cwd)
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
                ok = False
        return GateRun(ok=ok, results=results,
                       head_before=head_before, head_after=_head(cwd))
