"""ActionExecutor: a plain PROGRAM (not an agent).

Executes a fixed mapping from PlannerAction -> concrete AO CLI calls, with
idempotency (same action_id executes once) and budget enforcement
(max_local_fixes, max_replans, max_same_alerts from TaskSpec).

Forbidden: executing arbitrary Planner shell; handing Planner text to a shell;
auto-merge; deleting branches; modifying TaskSpec/tests; bypassing budgets.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

from .contracts import (PlannerAction, PlannerActionType, TaskSpec,
                        ProjectState)
from .state_store import StateStore


@dataclass
class ActionResult:
    action_id: str
    action: str
    ok: bool
    detail: str
    new_state: Optional[str] = None
    new_worker_session_id: Optional[str] = None


class ActionExecutor:
    def __init__(self, ao_bin: str, data_dir: str, run_file: str,
                 store: StateStore, worker_model: str = ""):
        self.ao_bin = ao_bin
        self.data_dir = data_dir
        self.run_file = run_file
        self.store = store
        # `ao spawn --model <m>`; empty = daemon default. The gateway default
        # (deepseek-v4-pro) 403s for claude-code workers, so configs pin
        # GLM-5.2 here (config/default.yaml worker.model).
        self.worker_model = worker_model or ""
        self.local_fixes = 0
        self.replans = 0

    def _spawn_args(self, project_id: str, harness: str, name: str,
                    prompt: str) -> list:
        args = ["spawn", "--kind", "worker", "--project", project_id,
                "--harness", harness, "--name", name,
                "--mode", "chat", "--prompt", prompt]
        if self.worker_model:
            args += ["--model", self.worker_model]
        return args

    def load_counters(self, task_id: str) -> None:
        """Reload budget counters from the persistent store (survives restart)."""
        self.local_fixes = self.store.counter_get("local_fixes:" + task_id)
        self.replans = self.store.counter_get("replans:" + task_id)

    def _env(self) -> Dict[str, str]:
        e = dict(os.environ)
        e["AO_DATA_DIR"] = self.data_dir
        e["AO_RUN_FILE"] = self.run_file
        return e

    def _run(self, args: list, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run([self.ao_bin] + args, capture_output=True,
                              text=True, timeout=timeout, env=self._env(),
                              encoding="utf-8", errors="replace")

    def execute(self, action: PlannerAction, task: TaskSpec) -> ActionResult:
        # Idempotency: never execute the same action_id twice.
        if self.store.action_executed(action.action_id):
            return ActionResult(action.action_id, action.action, True,
                                "already executed (idempotent)")
        self.load_counters(task.task_id)
        try:
            if action.action == PlannerActionType.CONTINUE:
                res = self._continue(action, task)
            elif action.action == PlannerActionType.SEND_LOCAL_FIX:
                res = self._send_local_fix(action, task)
            elif action.action == PlannerActionType.REPLAN_SPAWN:
                res = self._replan_spawn(action, task)
            elif action.action == PlannerActionType.CANDIDATE_DONE:
                res = ActionResult(action.action_id, action.action, True,
                                   "candidate done -> GATE_PENDING",
                                   new_state=ProjectState.GATE_PENDING)
            elif action.action == PlannerActionType.HUMAN:
                res = ActionResult(action.action_id, action.action, True,
                                   "halted for human",
                                   new_state=ProjectState.HUMAN)
            else:
                return ActionResult(action.action_id, action.action, False,
                                    "unknown action")
            self.store.mark_action_executed(action.action_id,
                                            {"ok": res.ok, "detail": res.detail})
            return res
        except Exception as e:
            return ActionResult(action.action_id, action.action, False,
                                "error: %s" % e)

    def _continue(self, action, task) -> ActionResult:
        return ActionResult(action.action_id, action.action, True,
                            "continue observing",
                            new_state=ProjectState.WORKER_RUNNING)

    def spawn_initial_worker(self, task: TaskSpec) -> Optional[str]:
        """Spawn the first worker for a task (no prior worker_session_id).

        Uses task.objective as the prompt and task.worker_harness (default
        claude-code; V0.1 froze CODEX_ONLY but the harness is now a TaskSpec
        field so the operator can switch without touching code).
        Returns the new session id or None on failure.
        """
        harness = getattr(task, "worker_harness", "claude-code") or "claude-code"
        prompt = ("Task: %s\n\nAcceptance criteria:\n%s\n\n"
                  "Work within allowed paths only. Do not modify tests or "
                  "forbidden paths. Run the gate command when ready."
                  % (task.objective,
                     "\n".join("- %s: %s" % (ac.id, ac.description)
                               for ac in task.acceptance_criteria)))
        proc = self._run(self._spawn_args(
            task.project_id, harness, ("worker-%s" % task.task_id)[:20],
            prompt))
        if proc.returncode != 0:
            return None
        import re
        m = re.search(r"spawned session (\S+)", proc.stdout or "")
        return m.group(1) if m else None

    def _send_local_fix(self, action, task) -> ActionResult:
        if self.local_fixes >= task.budgets["max_local_fixes"]:
            return ActionResult(action.action_id, action.action, False,
                                "max_local_fixes exceeded",
                                new_state=ProjectState.HUMAN)
        if not action.target_session_id:
            return ActionResult(action.action_id, action.action, False,
                                "no target_session_id", ProjectState.HUMAN)
        msg = action.message or ""
        # hard guard: never allow shell-ish content through
        if any(s in msg for s in ("&&", "|", "rm ", "del ", "Remove-Item")):
            return ActionResult(action.action_id, action.action, False,
                                "message rejected (shell-like content)",
                                ProjectState.HUMAN)
        proc = self._run(["send", "--session", action.target_session_id,
                          "--message", msg])
        ok = proc.returncode == 0
        self.local_fixes = self.store.counter_incr("local_fixes:" + task.task_id)
        return ActionResult(action.action_id, action.action, ok,
                            proc.stdout.strip()[:200] or proc.stderr.strip()[:200],
                            new_state=ProjectState.WORKER_RETRYING if ok
                            else ProjectState.HUMAN)

    def _replan_spawn(self, action, task) -> ActionResult:
        if self.replans >= task.budgets["max_replans"]:
            return ActionResult(action.action_id, action.action, False,
                                "max_replans exceeded",
                                new_state=ProjectState.HUMAN)
        spec = action.replacement_task_spec or {}
        prompt = spec.get("objective", task.objective)
        harness = getattr(task, "worker_harness", "claude-code") or "claude-code"
        # Stop the old worker before spawning a new one (re-route, not fork).
        # `ao session kill` terminates the session cleanly; the worktree is kept.
        old_sid = action.target_session_id or task.worker_session_id
        if old_sid:
            self._run(["session", "kill", old_sid], timeout=30)
        proc = self._run(self._spawn_args(
            task.project_id, harness, ("replan-%s" % task.task_id)[:20],
            prompt))
        ok = proc.returncode == 0
        new_sid = None
        if ok:
            import re
            m = re.search(r"spawned session (\S+)", proc.stdout or "")
            if m:
                new_sid = m.group(1)
        self.replans = self.store.counter_incr("replans:" + task.task_id)
        return ActionResult(action.action_id, action.action, ok,
                            proc.stdout.strip()[:200] or proc.stderr.strip()[:200],
                            new_state=ProjectState.WORKER_RUNNING if ok
                            else ProjectState.HUMAN,
                            new_worker_session_id=new_sid)

    def kill_worker(self, session_id: str) -> bool:
        """Stop a worker session cleanly (used by watchdog / replan)."""
        if not session_id:
            return False
        proc = self._run(["session", "kill", session_id], timeout=30)
        return proc.returncode == 0

    def nudge_worker(self, session_id: str, message: str) -> bool:
        """L0 fast path: send a lightweight hint to the worker WITHOUT
        consuming the local_fixes budget (distinct from SEND_LOCAL_FIX, which
        is a Planner-authorised fix and counts against max_local_fixes).

        Same shell-content guard as SEND_LOCAL_FIX; returns True on success.
        """
        if not session_id or not message:
            return False
        if any(s in message for s in ("&&", "|", "rm ", "del ", "Remove-Item")):
            return False
        proc = self._run(["send", "--session", session_id, "--message", message],
                         timeout=60)
        return proc.returncode == 0
