"""Integration Gate: run the pre-configured gate_commands in the Worker's
actual worktree and record exit code / stdout / stderr. All commands must
exit 0 to reach DONE. Gate failure -> INTEGRATION_FAILED evidence, re-enters
the loop (still bounded by budgets).

Gate NEVER runs commands invented by Auditor/Planner — only TaskSpec.gate_commands.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .contracts import TaskSpec
from .event_normalizer import now_iso
from .state_store import StateStore


@dataclass
class GateRun:
    ok: bool
    results: List[dict]

    def evidence(self) -> List[dict]:
        return [{"type": "integration_gate",
                 "summary": ("pass" if self.ok else "fail") +
                            " commands=" + str(len(self.results)),
                 "reference": "; ".join(
                     "exit=%d" % r["exit_code"] for r in self.results)}]


class IntegrationGate:
    def __init__(self, store: StateStore):
        self.store = store

    def run(self, task: TaskSpec, worktree_path: str) -> GateRun:
        results = []
        ok = True
        cwd = Path(worktree_path)
        for cmd in task.gate_commands:
            started = now_iso()
            try:
                proc = subprocess.run(cmd, shell=True, cwd=str(cwd),
                                       capture_output=True, text=True,
                                       timeout=300, encoding="utf-8",
                                       errors="replace")
                exit_code = proc.returncode
                stdout = (proc.stdout or "")[:8000]
                stderr = (proc.stderr or "")[:8000]
            except Exception as e:
                exit_code, stdout, stderr = -1, "", str(e)
            ended = now_iso()
            results.append({"command": cmd, "cwd": str(cwd),
                            "exit_code": exit_code, "stdout": stdout,
                            "stderr": stderr, "started_at": started,
                            "ended_at": ended})
            self.store.record_gate_run(task_id=task.task_id, command=cmd,
                cwd=str(cwd), exit_code=exit_code, started_at=started,
                ended_at=ended, stdout=stdout, stderr=stderr)
            if exit_code != 0:
                ok = False
        return GateRun(ok=ok, results=results)
