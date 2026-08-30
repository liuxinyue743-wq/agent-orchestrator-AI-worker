"""ActionExecutor: a plain PROGRAM (not an agent).

Executes a fixed mapping from PlannerAction -> concrete AO CLI calls, with
idempotency (same action_id executes once) and budget enforcement
(max_local_fixes, max_replans, max_same_alerts from TaskSpec).

Forbidden: executing arbitrary Planner shell; handing Planner text to a shell;
auto-merge; deleting branches; modifying TaskSpec/tests; bypassing budgets.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

from .mission_contracts import (PlannerAction, PlannerActionType, TaskSpec,
                        ProjectState)
from .event_normalizer import _epoch_seconds
from .state_store import StateStore


@dataclass
class ActionResult:
    action_id: str
    action: str
    ok: bool
    detail: str
    new_state: Optional[str] = None
    new_worker_session_id: Optional[str] = None


import re as _re

# Shell-ish content guard for Planner-authored messages (簇六). Word
# boundaries matter: the old substring check matched "del " inside
# "model ", so a Planner message containing the word 'model' was rejected
# as shell injection and routed straight to HUMAN.
_SHELLISH = _re.compile(
    r"(&&|\||\brm\b|\bdel\b|\bRemove-Item\b)", _re.IGNORECASE)


def _shellish(text: str) -> bool:
    return bool(_SHELLISH.search(text or ""))


class ActionExecutor:
    def __init__(self, ao_bin: str, data_dir: str, run_file: str,
                 store: StateStore, worker_model: str = "",
                 max_spawn_attempts: int = 3,
                 spawn_backoff_seconds: int = 30):
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
        # 簇五: bounded initial-spawn retries (real-run evidence: session-38
        # was rebuilt 3 times by the dispatch loop while the daemon kept
        # rejecting the spawn). Cap attempts per task with linear backoff;
        # the caller escalates to HUMAN once the cap is reached.
        self.max_spawn_attempts = int(max_spawn_attempts or 3)
        self.spawn_backoff_seconds = int(spawn_backoff_seconds or 30)

    def _spawn_args(self, project_id: str, harness: str, name: str,
                    prompt: str, include_model: bool = True) -> list:
        args = ["spawn", "--kind", "worker", "--project", project_id,
                "--harness", harness, "--name", name,
                "--mode", "chat", "--prompt", prompt]
        if include_model and self.worker_model:
            args += ["--model", self.worker_model]
        return args

    def _spawn(self, project_id: str, harness: str, name: str,
               prompt: str) -> Optional[str]:
        """Spawn a worker session; returns the new session id or None.

        The harness may reject an explicit `--model` value depending on its
        current login/config state (real-run evidence: the daemon accepted
        `--model GLM-5.2` for days, then started answering CHAT_CONTROLLER_
        FAILED 'Invalid value for config option model' while the same model
        still worked via the ANTHROPIC_MODEL env default). On that specific
        rejection, retry once WITHOUT --model — same effective model, zero
        operator intervention.
        """
        proc = self._run(self._spawn_args(project_id, harness, name, prompt))
        if proc.returncode != 0 and self.worker_model and \
                "config option model" in (proc.stderr or ""):
            proc = self._run(self._spawn_args(project_id, harness, name,
                                              prompt, include_model=False))
        if proc.returncode != 0:
            return None
        import re
        m = re.search(r"spawned session (\S+)", proc.stdout or "")
        return m.group(1) if m else None

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
        # Idempotency: never execute the same action_id twice. On a
        # crash-resume the state machine may sit in the action's pending
        # state while the side effect already happened; the early return
        # must still carry the new_state the original execution produced,
        # otherwise the loop parks forever (real-run evidence: LOCAL_FIX
        # executed, process killed before the WORKER_RETRYING transition).
        if self.store.action_executed(action.action_id):
            prev = self.store.action_executed_result(action.action_id) or {}
            ok = bool(prev.get("ok", True))
            resume_state = {
                PlannerActionType.CONTINUE: ProjectState.WORKER_RUNNING,
                PlannerActionType.SEND_LOCAL_FIX:
                    ProjectState.WORKER_RETRYING if ok else ProjectState.HUMAN,
                PlannerActionType.REPLAN_SPAWN:
                    ProjectState.WORKER_RUNNING if ok else ProjectState.HUMAN,
                PlannerActionType.CANDIDATE_DONE: ProjectState.GATE_PENDING,
                PlannerActionType.HUMAN: ProjectState.HUMAN,
            }.get(action.action)
            # 簇五: a crash AFTER a successful REPLAN spawn but BEFORE the
            # task re-bind must still hand the new worker id back, or the
            # resumed loop keeps tracking the killed old worker.
            return ActionResult(action.action_id, action.action, ok,
                                "already executed (idempotent)",
                                new_state=resume_state,
                                new_worker_session_id=prev.get(
                                    "new_worker_session_id"))
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
            self.store.mark_action_executed(
                action.action_id,
                {"ok": res.ok, "detail": res.detail,
                 # 簇五: persisted so a crash-resume idempotent return can
                 # hand the replacement worker id back to the loop.
                 "new_worker_session_id": res.new_worker_session_id})
            return res
        except Exception as e:
            return ActionResult(action.action_id, action.action, False,
                                "error: %s" % e)

    def _continue(self, action, task) -> ActionResult:
        return ActionResult(action.action_id, action.action, True,
                            "continue observing",
                            new_state=ProjectState.WORKER_RUNNING)

    def spawn_cap_reached(self, task_id: str) -> bool:
        """True when initial-spawn attempts for this task hit the cap."""
        return self.store.counter_get("spawn_attempts:" + task_id) >= \
            self.max_spawn_attempts

    def _spawn_backoff_pending(self, task_id: str) -> bool:
        next_at = self.store.counter_get("spawn_next_at:" + task_id)
        return bool(next_at) and _epoch_seconds() < next_at

    def spawn_initial_worker(self, task: TaskSpec) -> Optional[str]:
        """Spawn the first worker for a task (no prior worker_session_id).

        Uses task.objective as the prompt and task.worker_harness (default
        claude-code; V0.1 froze CODEX_ONLY but the harness is now a TaskSpec
        field so the operator can switch without touching code).
        Returns the new session id or None on failure.

        Bounded: at most `max_spawn_attempts` attempts per task with a
        linear backoff between them (attempt N waits N*backoff seconds).
        A successful spawn clears the counters so a later legitimately
        needed spawn is not blocked by ancient failures.
        """
        if self.spawn_cap_reached(task.task_id) or \
                self._spawn_backoff_pending(task.task_id):
            return None
        attempts = self.store.counter_incr("spawn_attempts:" + task.task_id)
        self.store.counter_set("spawn_next_at:" + task.task_id,
                               _epoch_seconds() +
                               self.spawn_backoff_seconds * attempts)
        harness = getattr(task, "worker_harness", "claude-code") or "claude-code"
        gate = "; ".join(task.gate_commands or [])
        prompt = ("Task: %s\n\nAcceptance criteria:\n%s\n\n"
                  "Work within allowed paths only. Do not modify tests or "
                  "forbidden paths. Run the gate command when ready.\n\n"
                  "Environment (do NOT waste turns exploring):\n"
                  "- Your working directory IS your private worktree; all "
                  "paths above are relative to it. Do not cd elsewhere.\n"
                  "- python and pytest are installed and on PATH. Use "
                  "`python -m pytest ...` directly.\n"
                  "- Create/edit files with the Write/Edit tools directly; "
                  "no need for ls/cat/command -v probing.\n"
                  "- The gate command for this task: %s\n"
                  "- If python/pytest turns out to be unavailable in YOUR "
                  "shell, do NOT probe the environment (no which/where/"
                  "python -c exploration): just complete the file edits and "
                  "reply DONE — an external deterministic gate runs the "
                  "tests authoritatively.\n"
                  "- When the gate is green, reply DONE and stop."
                  % (task.objective,
                     "\n".join("- %s: %s" % (ac.id, ac.description)
                               for ac in task.acceptance_criteria),
                     gate or "(none)"))
        sid = self._spawn(task.project_id, harness,
                          ("worker-%s" % task.task_id)[:20], prompt)
        if sid:
            # success: clear the retry counters so a future legitimately
            # needed spawn (e.g. after a replan kill) starts fresh.
            self.store.counter_delete_prefix("spawn_attempts:" + task.task_id)
            self.store.counter_delete_prefix("spawn_next_at:" + task.task_id)
        return sid

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
        if _shellish(msg):
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
        # Mission-level total-replan cap (budgets.max_total_replans): every
        # subtask of one mission shares ONE spawn budget so N subtasks can't
        # each burn max_replans spawns (N× the intended ceiling). Counted in
        # the store, so it survives restarts and applies across processes.
        parent = getattr(task, "subtask_of", None)
        if parent:
            limit = 0
            try:
                # read the parent mission's budget from its recorded spec
                row = self.store.load_task(parent)
                # mission budgets live on the MissionSpec, not TaskSpec —
                # read them from the missions table instead
                with self.store._lock:
                    cur = self.store._conn.execute(
                        "SELECT payload_json FROM missions WHERE mission_id=?",
                        (parent,))
                    r = cur.fetchone()
                if r:
                    limit = int((json.loads(r[0]).get("mission", {})
                                 .get("budgets", {})
                                 .get("max_total_replans", 0)) or 0)
            except Exception:
                limit = 0
            if limit > 0:
                key = "mission_replans:" + parent
                used = self.store.counter_get(key)
                if used >= limit:
                    return ActionResult(action.action_id, action.action,
                                        False,
                                        "mission max_total_replans exceeded "
                                        "(%d>=%d)" % (used, limit),
                                        new_state=ProjectState.HUMAN)
                self.store.counter_set(key, used + 1)
        spec = action.replacement_task_spec or {}
        prompt = spec.get("objective", task.objective)
        harness = getattr(task, "worker_harness", "claude-code") or "claude-code"
        # Stop the old worker before spawning a new one (re-route, not fork).
        # `ao session kill` terminates the session cleanly; the worktree is kept.
        old_sid = action.target_session_id or task.worker_session_id
        if old_sid:
            self._run(["session", "kill", old_sid], timeout=30)
        new_sid = self._spawn(task.project_id, harness,
                              ("replan-%s" % task.task_id)[:20], prompt)
        ok = new_sid is not None
        self.replans = self.store.counter_incr("replans:" + task.task_id)
        return ActionResult(action.action_id, action.action, ok,
                            ("spawned %s" % new_sid) if ok
                            else "replan spawn failed",
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
        if _shellish(message):
            return False
        proc = self._run(["send", "--session", session_id, "--message", message],
                         timeout=60)
        return proc.returncode == 0
