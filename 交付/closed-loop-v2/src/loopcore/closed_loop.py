"""Closed-loop controller.

Wires: Observer -> Auditor -> Planner -> ActionExecutor -> (loop) -> Gate.

A single pass (`step()`) reads fresh AO events for the bound Worker, runs the
Observer, and if a new alert fires builds an EvidenceBundle -> Auditor ->
Planner -> ActionExecutor. CANDIDATE_DONE triggers the Integration Gate;
Gate pass -> DONE.

State machine transitions are persisted. Budgets enforced. --dry-run runs the
whole pipeline up to (but not executing) ao send/spawn/gate.

Idempotency: each alert triggers at most one audit; each audit at most one
planner action; each action executed once. Process restart resumes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

from .action_executor import ActionExecutor, ActionResult
from .ao_adapter import AOAdapter
from .approvals import decide_approval
from .auditor import (AuditorProvider, ClaudeCliAuditorProvider,
                      EvidenceBundle, FakeAuditorProvider)
from .mission_contracts import (AuditDecision, AuditResult, PlannerAction,
                        PlannerActionType, ProjectState, TaskSpec,
                        is_legal_transition)
from .event_normalizer import (EventNormalizer, now_iso, make_id, stable_id,
                               _epoch_seconds)
from .mission_gate import IntegrationGate
from .event_observer import Observer
from .planner_adapter import (FakePlannerProvider, PlannerProvider,
                              AOOrchestratorPlannerProvider)
from .state_store import StateStore
from .verifier import FakeVerifierProvider, VerifierInput, VerifierProvider
from . import worktree as wt

RUNTIME = Path(__file__).resolve().parent.parent.parent / "runtime"


class ClosedLoop:
    def __init__(self, task: TaskSpec, cfg: Dict, *,
                 auditor: AuditorProvider,
                 planner: PlannerProvider,
                 executor: ActionExecutor,
                 observer: Observer,
                 adapter: AOAdapter,
                 gate: IntegrationGate,
                 store: StateStore,
                 verifier: Optional["VerifierProvider"] = None,
                 dry_run: bool = False,
                 instruct: str = ""):
        self.task = task
        self.cfg = cfg
        self.auditor = auditor
        self.planner = planner
        self.executor = executor
        self.observer = observer
        self.adapter = adapter
        self.gate = gate
        self.store = store
        self.dry_run = dry_run
        # Independent correctness verifier (read-only model role). When None
        # (legacy single-task runs), a default fake-free provider is created
        # lazily so DONE still requires an independent PASS — the deterministic
        # gate alone never declares a task finished.
        self.verifier = verifier
        # Top-level user directive the Planner absorbs and folds into its
        # strategy every cycle (the "leader" role lives in the Planner).
        self.instruct = instruct
        # Role-addressed user directives awaiting their next consumption
        # point (panel-posted mid-mission; owner-ruled: the user may address
        # ANY agent). Bounded to the last 20 per role.
        self.role_directives: Dict[str, List[str]] = {
            "auditor": [], "verifier": []}
        # When True (mission mode), the loop NEVER spawns its own initial
        # worker — the MissionController dispatches subtasks in dependency
        # order and sets worker_session_id itself.
        self.hold_spawn = False

    # ----------------------------------------------------------- state
    @property
    def state(self) -> str:
        s = self.store.latest_state(self.task.task_id)
        return s or ProjectState.TASK_READY

    def _transition(self, to: str, actor: str, reason: str,
                    evidence: Dict) -> None:
        frm = self.state
        if not is_legal_transition(frm, to):
            raise RuntimeError("illegal transition %s -> %s" % (frm, to))
        self.store.record_transition(task_id=self.task.task_id,
            from_state=frm, to_state=to, actor=actor, reason=reason,
            evidence=evidence)

    # ----------------------------------------------------------- step
    # Consecutive step() exceptions tolerated before halting to HUMAN. Each
    # failed tick that re-enters a pending state re-invokes an LLM role
    # (verifier/auditor/planner), so an UNBOUNDED retry burns up to
    # max_runtime_seconds/ poll_interval model calls ($0.20-capped each)
    # before the runtime watchdog fires. Three strikes is enough to tell a
    # persistent fault from a transient one (review: LOOP_ERROR escalation).
    MAX_CONSECUTIVE_LOOP_ERRORS = 3

    def step(self, injected_events: Optional[List] = None) -> Dict:
        """Top-level boundary: an unexpected error (illegal transition from a
        corrupted store, a None from a failed git probe, a transport hiccup)
        must NOT kill the mission runner thread — record it as a LOOP_ERROR
        alert and return an un-acted step; the next tick retries (review 簇五;
        real-run chain: _run_verifier TypeError -> step -> mission.step ->
        run_mission main loop -> dead runner).

        Bounded retries: MAX_CONSECUTIVE_LOOP_ERRORS consecutive failures
        halt the subtask to HUMAN (a persistent fault will never fix itself,
        and every retry costs a model call); a successful tick resets the
        counter."""
        try:
            result = self._step_impl(injected_events)
            self._loop_error_streak = 0
            return result
        except Exception as e:
            self._loop_error_streak = getattr(self, "_loop_error_streak", 0) + 1
            try:
                self.store.record_alert(
                    "LOOPERR-%s-%d" % (self.task.task_id, _epoch_seconds()),
                    {"alert_type": "LOOP_ERROR", "task_id": self.task.task_id,
                     "state": str(self.state), "error": str(e)[:500],
                     "consecutive": self._loop_error_streak})
            except Exception:
                pass
            try:
                state = str(self.state)
            except Exception:
                state = "?"
            if self._loop_error_streak >= self.MAX_CONSECUTIVE_LOOP_ERRORS:
                # Persistent fault: stop retrying (each retry re-invokes an
                # LLM role) and hand the subtask to a human.
                self._loop_error_streak = 0
                self._halt_budget(
                    "consecutive loop errors (%d): %s: %s"
                    % (self.MAX_CONSECUTIVE_LOOP_ERRORS,
                       type(e).__name__, str(e)[:200]))
                return {"state": str(self.state), "acted": True,
                        "error": "%s: %s" % (type(e).__name__, e)}
            return {"state": state, "acted": False,
                    "error": "%s: %s" % (type(e).__name__, e)}

    def _step_impl(self, injected_events: Optional[List] = None) -> Dict:
        result = {"state": self.state, "acted": False}
        if self.state in (ProjectState.DONE, ProjectState.HUMAN,
                          ProjectState.FAILED):
            return result
        # Runtime watchdog: enforce max_runtime_seconds (budget).
        if self._runtime_exceeded():
            result["acted"] = True
            self._halt_budget("max_runtime_seconds exceeded")
            result["state"] = self.state
            return result
        # Crash-resume: a process killed between the gate-pass transition and
        # the verifier call parks the loop in VERIFIER_PENDING forever (the
        # verifier ran inside _run_gate's synchronous step, and no branch
        # below re-enters it). If we ARE in VERIFIER_PENDING, the verifier
        # call by definition did not complete (a PASS would have moved to
        # DONE, a FAIL back to AUDIT_PENDING) — so re-enter it.
        if self.state == ProjectState.VERIFIER_PENDING:
            result["acted"] = True
            self._run_verifier()
            result["state"] = self.state
            return result
        # Crash-resume for the remaining transient "pending" states, same
        # pattern as VERIFIER_PENDING above: the synchronous chain
        # audit -> planner -> execute -> gate -> verifier can be killed at
        # ANY hop (tool timeout, Ctrl-C, power loss); without a re-entry
        # branch the loop then parks forever because nothing below picks it
        # up (real-run evidence: MISSION-QUICK-006 S2 parked in
        # PLANNER_PENDING after a mid-planner kill, 4+ min no progress).
        if self.state == ProjectState.AUDIT_PENDING:
            # Killed mid-audit (or between record_audit and the planner
            # transition): _completion_audit is re-entry safe — it skips the
            # transition when already in AUDIT_PENDING and re-runs the
            # deterministic gate capture + Auditor.
            result["acted"] = True
            self._completion_audit()
            result["state"] = self.state
            return result
        if self.state == ProjectState.PLANNER_PENDING:
            result["acted"] = True
            self._resume_planner()
            result["state"] = self.state
            return result
        if self.state in (ProjectState.LOCAL_FIX_PENDING,
                          ProjectState.REPLAN_PENDING):
            result["acted"] = True
            self._resume_action()
            if self.state in (ProjectState.DONE, ProjectState.HUMAN,
                              ProjectState.FAILED):
                result["state"] = self.state
                return result
            # fall through: the resume advanced (or kept) the machine in a
            # worker-active state — keep collecting this tick's events so the
            # alert-escalation cadence (max_same_alerts etc.) is unchanged.
        if self.state == ProjectState.GATE_PENDING:
            # Killed between the planner's CANDIDATE_DONE transition and the
            # gate run: the deterministic gate is side-effect free w.r.t. the
            # decision flow, so just re-run it.
            result["acted"] = True
            self._run_gate()
            result["state"] = self.state
            return result
        # Worker finished a local-fix retry: run a COMPLETION_AUDIT (capture
        # real test output -> Auditor decides) instead of jumping straight to
        # the gate. The Auditor's PASS routes through the Planner
        # (CANDIDATE_DONE) to the gate; LOCAL_FIX/REPLAN re-enter the loop.
        if self.state == ProjectState.WORKER_RETRYING:
            # Wait until the worker goes idle (fix may still be in-flight).
            ws = self._worker_status()
            if ws is None:
                # status query failed: UNKNOWN is not idle (fail-closed —
                # a transient AO/API error must never read as 'worker done';
                # review 簇四).
                return result
            act_state = (ws.get("activity") or {}).get("state") or \
                (ws.get("status") or "")
            if act_state not in ("idle", "waiting_input", "needs_input",
                                 "exited", "terminated"):
                # still busy — but a pending approval may be BLOCKING it:
                # resolve in-scope ones so the retry can proceed.
                if self._maybe_auto_approve():
                    result["acted"] = True
                return result  # worker still busy; poll again later
            # A worker parked on a pending permission request ALSO reports
            # waiting_input — that is a block, not completion (real-run bug:
            # MISSION-QUICK-008 S2 burned both LOCAL_FIX rounds auditing an
            # approval-blocked worker as "done"). Resolve in-scope approvals
            # and wait; never audit a blocked worker.
            if self._pending_approvals():
                if self._maybe_auto_approve():
                    result["acted"] = True
                return result
            result["acted"] = True
            self._completion_audit()
            result["state"] = self.state
            return result
        # No bound worker yet: spawn the first one (V0.1 single codex worker).
        # In mission mode the controller owns dispatch (dependency order);
        # an undispatched loop just waits.
        if not self.task.worker_session_id:
            if self.hold_spawn or self.dry_run:
                # held / dry-run: never spawn; stay ready.
                result["state"] = self.state
                return result
            new_sid = self.executor.spawn_initial_worker(self.task)
            if new_sid:
                self.task.worker_session_id = new_sid
                self.store.record_task(self.task.task_id, self.task.to_dict())
                self._transition(ProjectState.WORKER_RUNNING, "executor",
                                 "spawned initial worker %s" % new_sid,
                                 {"worker_session_id": new_sid})
                result["acted"] = True
            else:
                self._halt_budget("initial worker spawn failed")
            result["state"] = self.state
            return result
        # pull fresh events for the bound worker — or accept a pre-collected
        # batch injected by a MissionController (which polls the project event
        # stream ONCE per tick and routes items per session; avoids N×API
        # calls when N subtask loops share one project).
        if injected_events is not None:
            events = list(injected_events)
        elif self.task.worker_session_id:
            events = self._collect_events(self.task.worker_session_id)
        else:
            events = []
        # bounded auto-approval FIRST (review 簇二): a worker parked on a
        # pending permission request is BLOCKED, not stalled. Resolve
        # in-scope requests before any alert math, and pause observation
        # while blocked so blocked time never counts toward NO_PROGRESS.
        # A block that outlives blocked_escalation_seconds escalates to
        # HUMAN (bounded — never an infinite silent wait).
        if self._pending_approvals():
            if self._maybe_auto_approve():
                result["acted"] = True
            if self._blocked_too_long():
                result["acted"] = True
                self._halt_budget("worker blocked on an unresolved approval "
                                  "beyond blocked_escalation_seconds")
            result["state"] = self.state
            return result
        self.store.counter_delete_prefix("blocked_since:" + self.task.task_id)
        new_alerts = []
        fresh_errors = []
        for ev in events:
            new_alerts += self.observer.feed(ev)
            if getattr(ev, "event_type", None) == "error" and \
                    getattr(ev, "activity", False):
                fresh_errors.append(ev)
        if self.state == ProjectState.TASK_READY and events:
            self._transition(ProjectState.WORKER_RUNNING, "observer",
                             "worker activity observed", {})
        if new_alerts:
            # L1: project-level semantic failure -> Auditor + Planner.
            result["acted"] = True
            self._handle_alerts(new_alerts, events)
        else:
            acted_l0 = False
            if fresh_errors and self.state == ProjectState.WORKER_RUNNING:
                # L0: a single/local execution failure (not yet repeated).
                # Route a short nudge back to the current Worker WITHOUT
                # escalating to the Auditor; repeated errors (L1) supersede.
                acted_l0 = self._maybe_l0_nudge(fresh_errors)
                result["acted"] = result["acted"] or acted_l0
            if not acted_l0 and self._maybe_idle_completion(events):
                # Quiet completion (review 簇二 elif-shadowing: a worker that
                # once errored must still reach the completion audit — only
                # an ACTED L0 nudge defers it, not the mere presence of a
                # historical error event).
                result["acted"] = True
        # bounded auto-approval: a worker blocked on a pending permission
        # request would sit forever in an unattended mission. Allow_once is
        # granted ONLY for file edits inside allowed_paths; anything else
        # (tests, forbidden paths, outside paths) stays pending for a human.
        if self._maybe_auto_approve():
            result["acted"] = True
        result["state"] = self.state
        return result

    # ------------------------------------------------ bounded auto-approval
    def _pending_approvals(self) -> List[Dict]:
        """Pending approval activities of the bound worker.

        A worker blocked on a permission prompt reports status
        waiting_input — indistinguishable from 'idle/done' without this
        check, so every idle-as-completion decision must consult it first.
        """
        if self.dry_run or not self.task.worker_session_id:
            return []
        try:
            conv = self.adapter.get_worker_conversation(
                self.task.worker_session_id)
        except Exception:
            return []
        return [a for a in (conv.get("activities") or [])
                if (a.get("activityKind") or a.get("kind")) == "approval"
                and (a.get("status") or "") == "pending"]

    def _blocked_too_long(self) -> bool:
        """True when the worker has sat on unresolved approvals longer than
        observer.blocked_escalation_seconds (default 600). First blocked tick
        stamps the clock; returning False means 'still within grace'."""
        limit = int(self.cfg.get("observer", {}).get(
            "blocked_escalation_seconds", 600) or 600)
        if limit <= 0:
            return False
        key = "blocked_since:" + self.task.task_id
        started = self.store.counter_get(key)
        now = _epoch_seconds()
        if not started:
            self.store.counter_set(key, now)
            return False
        return (now - int(started)) > limit

    def _maybe_auto_approve(self) -> bool:
        """Resolve pending approvals for edits inside allowed_paths.

        The claude-code harness asks for permission before every Edit/Write;
        in an unattended mission nobody answers and the worker stalls with a
        turn in flight (real-run evidence: 30 min stall -> budget HUMAN).
        Policy (user-approved): file edits whose target resolves inside the
        task's allowed_paths are resolved as allow_once via the daemon REST
        API. Forbidden/outside paths are left pending — the human touchpoint.
        Idempotent: resolved requests disappear from the pending set; each
        request id is additionally deduped in a store counter.
        """
        if self.dry_run or not self.task.worker_session_id:
            return False
        # TASK_READY included (簇八, real-run MISSION-QUICK-014 S1): a worker
        # blocked on a permission prompt in its FIRST turn never emits the
        # activity that would transition TASK_READY->WORKER_RUNNING — if the
        # state gate excludes TASK_READY, the blocked-pause branch parks the
        # loop while the resolvable approval sits untouched until HUMAN.
        if self.state not in (ProjectState.TASK_READY,
                              ProjectState.WORKER_RUNNING,
                              ProjectState.WORKER_RETRYING):
            return False
        acted = False
        worktree = self._worktree_path() or ""
        for act in self._pending_approvals():
            req_id = act.get("providerItemId") or act.get("id")
            if not req_id:
                continue
            key = "approved:" + self.task.task_id + ":" + req_id
            if self.store.counter_get(key) != 0:
                continue
            detail = act.get("detail") or {}
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except Exception:
                    detail = {}
            inp = detail.get("input") or {}
            fpath = inp.get("file_path") or inp.get("path") or ""
            if not fpath:
                # command approval: the richer local policy stays (cd-prefix
                # and subshell unwrapping, read-only whitelist need worktree
                # context approvals.is_safe_command does not have).
                cmd = str(inp.get("command") or "")
                allow = bool(cmd) and self._is_gate_command(cmd)
            else:
                # file-edit approval: resolved by the pure, unit-tested
                # policy in approvals.py (proper Path.resolve().relative_to()
                # — no string-marker hacks, no basename fallback: a path that
                # does not resolve inside the worktree is simply DENIED).
                decision = decide_approval(
                    act, allowed_paths=list(self.task.allowed_paths),
                    forbidden_paths=list(self.task.forbidden_paths),
                    gate_commands=[], worktree_root=worktree)
                allow = bool(decision and decision.allow)
            if allow:
                ok = self.adapter.resolve_approval(
                    self.task.worker_session_id, req_id, "allow")
                self.store.counter_set(key, 1 if ok else -1)
                acted = acted or ok
            else:
                # out-of-scope request: record and leave pending for human
                self.store.counter_set(key, -1)
        return acted

    def _is_gate_command(self, cmd: str) -> bool:
        """True when a requested command may run unattended.

        Allowed (bounded auto-approval policy, user-approved):
          - the task's own gate commands, in any -q/-v verbosity variant
          - pytest invocations (test execution never edits sources)
          - read-only exploration inside the worktree (ls/dir/pwd/cat/type/
            head/tail, command -v / where / which, python --version)
          - read/add/commit git bookkeeping INSIDE the worker worktree
          - `cd <inside-worktree> &&` prefixes around any of the above
        Everything else (arbitrary shell, network, package installs, file
        deletion, redirection/pipes that could smuggle writes) stays pending
        for the human.
        """
        cmd = " ".join((cmd or "").split())
        if not cmd:
            return False
        # Chained commands: EVERY segment must be independently safe, and no
        # segment may contain redirection/pipe/semicolon smuggling. A single
        # '&' is shell backgrounding/chaining (`git status & curl evil.com`)
        # and must NOT survive the `&&` split — reject it per segment.
        segments = [s.strip() for s in cmd.split("&&")]
        if any(not s for s in segments):
            return False
        for seg in segments:
            if any(tok in seg for tok in (">", "<", "|", ";", "`", "$(",
                                          "&")):
                return False
            if not self._is_safe_segment(seg):
                return False
        return True

    def _is_safe_segment(self, seg: str) -> bool:
        """One `&&`-free command segment judged against the safe list."""
        import re
        # unwrap one layer of subshell parentheses: `(command -v python)`
        m = re.match(r'^\((.*)\)$', seg)
        if m:
            seg = " ".join(m.group(1).split())
            if not seg:
                return False
        m = re.match(r'^cd\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))\s*$', seg)
        if m:
            # a bare cd is only meaningful as a prefix; allow it only when
            # it targets the worker's own worktree.
            return self._cd_inside_worktree(
                m.group(1) or m.group(2) or m.group(3) or "")
        for g in self.task.gate_commands or []:
            g = " ".join(str(g).split())
            if not g:
                continue
            if seg == g or seg.startswith(g + " ") or g.startswith(seg + " ") \
                    or seg.rstrip(" -v").rstrip(" -q") == g.rstrip(" -q"):
                return True
        head = seg.split(" ", 1)[0]
        rest = seg[len(head):].strip()
        if head == "pytest":
            return True
        if head in ("python", "python3", "py"):
            if rest.startswith("-m pytest"):
                return True
            if rest in ("--version", "-V", "-c \"import sys\""):
                return True
            return False
        if head in ("ls", "dir", "pwd", "cat", "type", "head", "tail",
                    "more"):
            return True
        if head in ("command", "where", "which"):
            # command -v python / where pytest: environment discovery
            return True
        if head == "git":
            sub = seg.split(" ")[1:2]
            if sub and sub[0] in ("add", "commit", "status", "diff", "log",
                                  "restore", "checkout"):
                # restore/checkout confined to the worker's own worktree;
                # the path gate + frozen-base diff still bound any escape
                return True
        return False

    def _cd_inside_worktree(self, target: str) -> bool:
        """True when a `cd` target resolves to the worker's own worktree
        (or a subdirectory of it)."""
        import os
        wt = self._worktree_path()
        if not wt or not target:
            return False
        t = os.path.normcase(os.path.normpath(str(target)))
        w = os.path.normcase(os.path.normpath(str(wt)))
        try:
            return os.path.commonpath([t, w]) == w
        except ValueError:
            return False

    # ----------------------------------------------------------- L0 fast path
    def _maybe_l0_nudge(self, fresh_errors: List) -> bool:
        """L0 local-failure fast path: one nudge per fingerprint per task.

        Sends a brief 'you hit an error, retry/fix locally' hint to the worker
        so a single flaky/test failure is handled in-Worker without invoking
        the Auditor. Returns True if a nudge was actually sent this step.

        Hard limits so it never becomes a loop or shadows L1:
          - at most one nudge per error fingerprint (deduped in `l0_nudged:`)
          - only in WORKER_RUNNING (L1 alert handling otherwise takes over)
          - dry-run never sends
          - shell-content guard (same as SEND_LOCAL_FIX)
        """
        if self.dry_run or not self.task.worker_session_id:
            return False
        # never interrupt a turn in flight: AO rejects mid-turn sends with
        # "ACP conversation already has a turn in flight" and the rejected
        # send can kill the controller ("controller ended before the turn
        # completed"). Wait until the worker is idle.
        ws = self._worker_status() or {}
        act_state = (ws.get("activity") or {}).get("state") or \
            (ws.get("status") or "")
        if act_state not in ("idle", "exited", "terminated", ""):
            return False
        # hatch grace: a freshly-spawned worker's early tool "errors" are
        # usually self-correction noise (e.g. edit-before-read rejections);
        # nudging 20s in makes the mission fight its own workers. Anchored to
        # the worker's hatch time (separate from the watchdog's started_at,
        # which may predate the spawn on a resumed store).
        grace = int(self.cfg.get("observer", {}).get(
            "l0_nudge_grace_seconds", 300) or 300)
        hatch_key = "hatched_at:" + self.task.task_id + ":" + \
            self.task.worker_session_id
        hatched = self.store.counter_get(hatch_key)
        if not hatched:
            self.store.counter_set(hatch_key, _epoch_seconds())
            return False               # first poll after bind: wait
        if (_epoch_seconds() - int(hatched)) < grace:
            return False
        sent = False
        for e in fresh_errors:
            fp = getattr(e, "fingerprint", "") or getattr(e, "event_id", "")
            key = "l0_nudged:" + self.task.task_id + ":" + fp
            if self.store.counter_get(key) > 0:
                continue
            msg = ("You hit an error. Re-read the failing output and the "
                   "acceptance criteria, then retry. Do not modify tests or "
                   "forbidden paths.")
            ok = self.executor.nudge_worker(self.task.worker_session_id, msg)
            self.store.counter_set(key, 1)
            from .mission_contracts import PlannerAction, PlannerActionType
            pa = PlannerAction(
                action_id=stable_id("L0", fp, length=12),
                task_id=self.task.task_id,
                action=PlannerActionType.SEND_LOCAL_FIX,
                reason="L0 local-failure nudge (no audit, no budget)",
                target_session_id=self.task.worker_session_id, message=msg)
            self.store.record_action(pa.action_id, self.task.task_id,
                                     pa.to_dict())
            sent = True
        return sent

    # ------------------------------------------------- quiet-completion path
    def _maybe_idle_completion(self, events: List) -> bool:
        """Worker idle + no alerts + no fresh errors -> COMPLETION_AUDIT once.

        Covers the first-try-success worker (task done, nothing ever fired) and
        the silent stall: both end in an idle worker with the loop parked in
        WORKER_RUNNING and no path to DONE. One completion audit decides —
        gate evidence in hand, Auditor judgment routes PASS->gate->DONE or
        escalates. Paced by `idle_audit_cooldown_seconds` (default 300) so a
        long-running healthy worker isn't audited every poll.
        """
        if self.state not in (ProjectState.WORKER_RUNNING,
                              ProjectState.AUDIT_PENDING) or self.dry_run:
            return False
        if not self.task.worker_session_id or not events:
            return False
        ws = self._worker_status()
        if ws is None:
            return False  # status unknown: never audit as 'idle' (fail-closed)
        act_state = (ws.get("activity") or {}).get("state") or \
            (ws.get("status") or "")
        if act_state not in ("idle", "waiting_input", "needs_input",
                             "exited", "terminated"):
            return False  # still working; wait
        # waiting_input can also mean "blocked on a permission prompt" —
        # resolve in-scope approvals instead of auditing a blocked worker.
        if self._pending_approvals():
            self._maybe_auto_approve()
            return False
        cooldown = int(self.cfg.get("observer", {}).get(
            "idle_audit_cooldown_seconds", 300) or 300)
        last = self.store.counter_get("last_audit_at:" + self.task.task_id)
        now = _epoch_seconds()
        if last and (now - int(last)) < cooldown:
            return False
        self.store.counter_set("last_audit_at:" + self.task.task_id, now)
        self._completion_audit()
        return True

    # ----------------------------------------------------------- budgets
    def _runtime_exceeded(self) -> bool:
        limit = int(self.task.budgets.get("max_runtime_seconds", 0) or 0)
        if limit <= 0:
            return False
        started = self.store.counter_get("started_at:" + self.task.task_id)
        if not started:
            # The budget clock starts at DISPATCH, not at first step: an
            # undispatched subtask (waiting on dependencies or a failed
            # spawn) must not burn its runtime budget (review 簇二; real-run
            # evidence: MISSION-QUICK-011 S2 burned 30 min of budget during
            # a spawn outage, then HUMANed seconds after its worker started).
            if not self.task.worker_session_id:
                return False
            self.store.counter_set("started_at:" + self.task.task_id,
                                   _epoch_seconds())
            return False
        return (_epoch_seconds() - int(started)) > limit

    def _halt_budget(self, reason: str) -> None:
        """Transition to HUMAN on a budget/limit breach, stopping the worker."""
        if self.task.worker_session_id:
            try:
                self.executor.kill_worker(self.task.worker_session_id)
            except Exception:
                pass
        if is_legal_transition(self.state, ProjectState.HUMAN):
            self._transition(ProjectState.HUMAN, "budget", reason, {})
        elif is_legal_transition(self.state, ProjectState.FAILED):
            self._transition(ProjectState.FAILED, "budget", reason, {})

    def _same_alert_count(self, alert) -> int:
        """How many alerts of this fingerprint have fired (persistent)."""
        key = "same_alert:" + self.task.task_id + ":" + \
            (alert.error_fingerprint or alert.alert_type)
        return self.store.counter_get(key)

    def _bump_same_alert(self, alert) -> int:
        key = "same_alert:" + self.task.task_id + ":" + \
            (alert.error_fingerprint or alert.alert_type)
        return self.store.counter_incr(key)

    def _collect_events(self, worker_id: str) -> List:
        pid = self.task.project_id
        items = self.adapter.get_recent_events(pid, since=0)
        turn_times: Dict[str, Dict[str, str]] = {}
        evs = []
        for item in items:
            if item["kind"] == "turn":
                t = item["turn"]
                turn_times.setdefault(item["session_id"], {})[str(t.get("id"))] \
                    = t.get("requestedAt") or t.get("completedAt")
        for item in items:
            if item.get("session_id") != worker_id:
                # still feed session-level events for this worker
                if item["kind"] == "session" and \
                        item["session"].get("id") == worker_id:
                    evs += self.normalizer().from_session(item["session"])
                continue
            if item["kind"] == "session":
                evs += self.normalizer().from_session(item["session"])
            elif item["kind"] == "turn":
                evs += self.normalizer().from_turn(worker_id, pid, item["turn"])
            elif item["kind"] == "activity":
                evs += self.normalizer().from_activity(
                    worker_id, pid, item["activity"],
                    turn_times.get(worker_id, {}), None)
        return evs

    def normalizer(self) -> EventNormalizer:
        n = EventNormalizer(self.cfg.get("fingerprint"),
                            bool(self.cfg.get("observer", {}).get(
                                "turn_diff_counts_as_progress", True)))
        return n

    # ----------------------------------------------------------- alert->audit
    def _audit_id_for(self, al) -> str:
        return stable_id("AUDIT", al.alert_id, length=16)

    def _primary_alert(self, alerts: List):
        """Headline alert for an aggregated incident (most severe first)."""
        def severity(al):
            et = getattr(al, "alert_type", "")
            if et == "REPEATED_ERROR":
                return (2, getattr(al, "error_count", 0) or 0)
            if et == "NO_PROGRESS":
                return (1, 0)
            return (0, 0)
        return max(alerts, key=severity)

    def _handle_alerts(self, alerts: List, events: Optional[List] = None) -> None:
        """One observation cycle -> ONE incident -> ONE audit -> ONE action.

        A burst of N alerts in a single poll must NOT fan out into N audits and
        N planner actions (the old per-alert loop re-audited the same failure
        over and over — the dead-loop driver). Aggregation + a wait period are
        the anti-dead-loop core:

          - drop already-audited alerts (idempotent across restarts)
          - honour the audit wait period: never re-audit until the previous fix
            has had time to land (new alerts during the wait are expected noise)
          - cap per-fingerprint escalation (max_same_alerts) -> HUMAN
          - audit ONE aggregated bundle; each fired alert is recorded against
            the same incident result so none re-triggers.
        """
        if not alerts:
            return
        fresh = [al for al in alerts
                 if not self.store.audit_seen(self._audit_id_for(al))]
        if not fresh:
            return
        # max_same_alerts cap fires EVEN during the wait period: the hard cap
        # must break a repeating-fingerprint loop regardless of audit pacing.
        limit = int(self.task.budgets.get("max_same_alerts", 0) or 0)
        for al in fresh:
            if limit > 0:
                n = self._bump_same_alert(al)
                if n > limit:
                    self._halt_budget("max_same_alerts exceeded (%d>%d, fp=%s)"
                                      % (n, limit,
                                         getattr(al, "error_fingerprint", "")))
                    return
        # Wait period: pace audits (not the hard cap) so a fix has time to
        # land before the next audit cycle re-escalates.
        cooldown = int(self.cfg.get("observer", {}).get(
            "audit_cooldown_seconds", 60) or 60)
        last = self.store.counter_get("last_audit_at:" + self.task.task_id)
        now = _epoch_seconds()
        if last and (now - int(last)) < cooldown:
            return
        self.store.counter_set("last_audit_at:" + self.task.task_id, now)
        primary = self._primary_alert(fresh)
        if self.state != ProjectState.AUDIT_PENDING:
            self._transition(ProjectState.AUDIT_PENDING, "observer",
                             "%d alert(s) -> one aggregated audit" % len(fresh),
                             {"alert_ids": [a.alert_id for a in fresh]})
        bundle = self._build_bundle(primary, events or [], all_alerts=fresh)
        audit = self.auditor.audit(bundle, self._audit_id_for(primary))
        # Record the incident result against EVERY fired alert so none of them
        # re-triggers a second audit (each row links to the same incident).
        for al in fresh:
            self.store.record_audit(self._audit_id_for(al), self.task.task_id,
                                    audit.to_dict())
        self._to_planner(audit)

    def _build_bundle(self, alert, events: Optional[List] = None,
                      all_alerts: Optional[List] = None) -> EvidenceBundle:
        events = events or []
        all_alerts = all_alerts or ([alert] if alert else [])
        # Capture REAL test output for the Auditor (P0: was always empty). Run
        # the acceptance commands read-only; if no worktree/gate is runnable,
        # fall back to the alert-implied failed criteria.
        run, test_output = self._run_gate_capture()
        if run is not None:
            satisfied, failed = self._ac_from_gate(run)
        else:
            satisfied, failed = [], self._failed_ac_from_alert(alert)
        return EvidenceBundle(
            task_spec=self.task.to_dict(),
            worker_id=self.task.worker_session_id,
            subtask_id=getattr(self.task, "subtask_of", None),
            alert=alert.to_dict() if hasattr(alert, "to_dict") else dict(alert),
            alerts=[a.to_dict() if hasattr(a, "to_dict") else dict(a)
                    for a in all_alerts],
            events=[e.to_dict() if hasattr(e, "to_dict") else dict(e)
                    for e in events[-10:]],
            worker_status=self._worker_status(),
            git_diff=self._git_diff(),
            test_output=test_output,
            satisfied_criteria=satisfied,
            failed_criteria=failed,
            history={"local_fixes": self.executor.local_fixes,
                     "replans": self.executor.replans,
                     # user directives addressed to the Auditor (panel
                     # channel): the prompt builder embeds the bundle, so
                     # the Auditor sees them with the evidence.
                     "user_directives": self.role_directives["auditor"][-10:]})

    def _worker_status(self) -> Optional[Dict]:
        if not self.task.worker_session_id:
            return None
        try:
            return self.adapter.get_worker_status(self.task.worker_session_id)
        except Exception:
            return None

    def _base_commit(self) -> str:
        """Frozen base commit for progress/path diffing (worker commits cannot
        hide edits already made relative to this reference)."""
        worktree = self._worktree_path()
        if not worktree:
            return ""
        try:
            # scope = bound worker session: isolates concurrent workers on the
            # same task (each freezes against its OWN worktree's HEAD).
            return wt.freeze_base(worktree, self.store, self.task.task_id,
                                  scope=self.task.worker_session_id or "")
        except Exception:
            return ""

    def _git_diff(self) -> str:
        worktree = self._worktree_path()
        if not worktree:
            return ""
        base = self._base_commit()
        if not base:
            # fail-closed: an unknown base must be VISIBLE to the Auditor,
            # not silently rendered as an empty (clean-looking) diff.
            return "[loopcore] diff unavailable: base commit unknown\n"
        try:
            return wt.git_diff_text(worktree, base)
        except Exception:
            return "[loopcore] diff unavailable (error)\n"

    def _failed_ac_from_alert(self, alert) -> List[str]:
        # Without test output, a REPEATED_ERROR/NO_PROGRESS alert conservatively
        # implies all ACs unmet (the Auditor may narrow this from its own read).
        return [ac.id for ac in self.task.acceptance_criteria]

    def _run_gate_capture(self):
        """Run TaskSpec.gate_commands to capture real test output.

        Read-only with respect to the decision flow: does NOT transition state.
        Returns (GateRun|None, test_output_str). Gate commands are the ONLY
        commands ever run here — never anything invented by Auditor/Planner.
        """
        worktree = self._worktree_path()
        if not worktree or not self.task.gate_commands:
            return None, ""
        try:
            run = self.gate.run(self.task, worktree)
            test_output = "\n".join((r.get("stdout") or "") + (r.get("stderr") or "")
                                    for r in run.results)
            return run, test_output
        except Exception:
            return None, ""

    def _ac_from_gate(self, run):
        """Map a gate run to (satisfied, failed) criteria ids.

        We cannot parse test output into individual ACs, so a green gate means
        every AC is satisfied; a red gate means every AC is conservatively
        unmet (the Auditor narrows the real set from the captured output).
        """
        if run is None:
            return [], []
        ids = [ac.id for ac in self.task.acceptance_criteria]
        return (ids, []) if run.ok else ([], ids)

    # ----------------------------------------------------------- planner->action
    def _resume_planner(self) -> None:
        """Crash-resume from PLANNER_PENDING: re-run the planner for the
        latest recorded audit. The action id is deterministic
        (ACTION-<audit suffix>) and record_action is INSERT OR IGNORE, so a
        resume after a partial write stays consistent; executor-side
        idempotency (executed_actions) prevents double side effects.
        """
        payload = self.store.latest_audit(self.task.task_id)
        if not payload:
            # No audit survived the crash: fall back to a fresh completion
            # audit so the Auditor re-derives the decision from evidence.
            self._completion_audit()
            return
        self._to_planner(AuditResult.from_dict(payload))

    def _resume_action(self) -> None:
        """Crash-resume from LOCAL_FIX_PENDING / REPLAN_PENDING: reload the
        latest planner action and re-execute it. The executor dedupes by
        action_id and, on an already-executed action, returns the state the
        original run produced — so the machine advances exactly once.
        """
        payload = self.store.latest_action(self.task.task_id)
        if not payload:
            # Transition happened before record_action: rebuild via planner.
            self._resume_planner()
            return
        self._execute(PlannerAction.from_dict(payload), None)

    def _to_planner(self, audit: AuditResult) -> None:
        if self.state != ProjectState.PLANNER_PENDING:
            self._transition(ProjectState.PLANNER_PENDING, "auditor",
                             "audit decision=%s" % audit.decision,
                             {"audit_id": audit.audit_id})
        # A PASS means the worker recovered: wipe its same-alert escalation
        # debt (review 簇二 — lifetime accumulation made one fixed incident
        # count against the worker forever).
        if audit.decision == AuditDecision.PASS:
            self.store.counter_delete_prefix(
                "same_alert:" + self.task.task_id + ":")
        self.executor.load_counters(self.task.task_id)
        action_id = stable_id("ACTION", audit.audit_id, length=16)
        board = self.board() if callable(getattr(self, "board", None)) \
            else None
        pa = self.planner.plan(audit, self.task.to_dict(), action_id,
            target_session_id=self.task.worker_session_id,
            remaining_replans=max(0, self.task.budgets["max_replans"]
                                   - self.executor.replans),
            instruct=self.instruct, board=board)
        ok, msg = pa.validate()
        if not ok:
            pa = PlannerAction(action_id=action_id, task_id=self.task.task_id,
                action="HUMAN", reason="invalid planner action: %s" % msg)
        self.store.record_action(action_id, self.task.task_id, pa.to_dict())
        self._execute(pa, audit)

    def _execute(self, pa: PlannerAction, audit: AuditResult) -> None:
        if self.dry_run:
            # dry-run: do not call ao send/spawn/gate
            return
        # Advance the state machine through the planner's chosen pending state.
        pending = {
            PlannerActionType.SEND_LOCAL_FIX: ProjectState.LOCAL_FIX_PENDING,
            PlannerActionType.REPLAN_SPAWN: ProjectState.REPLAN_PENDING,
            PlannerActionType.CANDIDATE_DONE: ProjectState.GATE_PENDING,
            PlannerActionType.CONTINUE: ProjectState.WORKER_RUNNING,
            PlannerActionType.HUMAN: ProjectState.HUMAN,
        }.get(pa.action)
        if pending and is_legal_transition(self.state, pending):
            self._transition(pending, "planner", pa.reason,
                             {"action_id": pa.action_id})
        res: ActionResult = self.executor.execute(pa, self.task)
        if res.new_state:
            if is_legal_transition(self.state, res.new_state):
                self._transition(res.new_state, "action_executor",
                                 res.detail, {"action_id": pa.action_id})
        # REPLAN_SPAWN routed the task to a fresh worker: write its session id
        # back into the TaskSpec so subsequent observation tracks the NEW
        # worker, not the dead old one (P0: the id was previously dropped).
        if res.new_worker_session_id:
            self.task.worker_session_id = res.new_worker_session_id
            self.store.record_task(self.task.task_id, self.task.to_dict())
        # candidate done -> gate
        if pa.action == PlannerActionType.CANDIDATE_DONE and res.ok:
            self._run_gate()

    # ----------------------------------------------------------- gate
    def _run_gate(self) -> None:
        worktree = self._worktree_path()
        if not worktree:
            self._transition(ProjectState.HUMAN, "gate",
                            "no worktree path resolvable", {})
            return
        # Enforce path gate BEFORE running the gate: a worker that edited
        # tests / forbidden paths, or strayed outside allowed paths, must never
        # pass by self-modifying the ACs.
        forbidden, outside = self._path_violations()
        violations = forbidden + outside
        if violations:
            self.store.record_gate_run(task_id=self.task.task_id,
                command="path-gate", cwd=worktree, exit_code=1,
                started_at=now_iso(), ended_at=now_iso(), stdout="",
                stderr="path violations: " + ", ".join(violations))
            self._transition(ProjectState.HUMAN, "gate",
                "path violations: %s" % ", ".join(violations),
                {"forbidden": forbidden, "outside_allowed": outside})
            return
        run = self.gate.run(self.task, worktree)
        if self.dry_run:
            return
        target = ProjectState.VERIFIER_PENDING if run.ok \
            else ProjectState.AUDIT_PENDING
        if is_legal_transition(self.state, target):
            self._transition(target, "integration_gate",
                             "gate %s" % ("pass" if run.ok else "fail"),
                             {"evidence": run.evidence()})
        if run.ok:
            # 簇二: a green integration gate IS proven progress — emit a
            # strong-progress event so the NO_PROGRESS clock closes for this
            # task (in strong mode this is the production emitter that makes
            # the mode usable; in weak mode it is harmless extra evidence).
            try:
                self.observer.feed(
                    self.normalizer().make_strong_progress_event(
                        project_id=self.task.project_id,
                        worker_id=self.task.worker_session_id,
                        task_id=self.task.task_id,
                        timestamp=now_iso(),
                        message="integration gate pass", source="gate"))
            except Exception:
                pass
            # gate commands passed — but the deterministic gate alone never
            # declares DONE: route through the independent Verifier.
            self._run_verifier(run)
        else:
            # re-enter audit with gate evidence (bounded by budgets)
            audit_id = make_id("AUDIT-GATE")
            if not self.store.audit_seen(audit_id):
                bundle = EvidenceBundle(
                    task_spec=self.task.to_dict(), alert=None,
                    worker_id=self.task.worker_session_id,
                    subtask_id=getattr(self.task, "subtask_of", None),
                    worker_status=self._worker_status(),
                    git_diff=self._git_diff(),
                    failed_criteria=[ac.id for ac in self.task.acceptance_criteria],
                    test_output="\n".join(r["stdout"] + r["stderr"]
                                          for r in run.results),
                    history={"local_fixes": self.executor.local_fixes,
                             "replans": self.executor.replans,
                             "user_directives":
                                 self.role_directives["auditor"][-10:]})
                audit = self.auditor.audit(bundle, audit_id)
                self.store.record_audit(audit_id, self.task.task_id,
                                        audit.to_dict())
                self._to_planner(audit)

    # ----------------------------------------------------------- verifier
    def _run_verifier(self, run=None) -> None:
        """Independent verification: gate passed -> is the result CORRECT?

        Assembles trusted inputs (diff vs frozen base, REAL gate output,
        changed paths, deterministic path-gate findings) and asks the
        read-only Verifier. PASS -> DONE; FAIL -> back to AUDIT_PENDING with
        the verifier findings as evidence (the Planner then decides how to
        fix — bounded by budgets).
        """
        from .mission_contracts import VerifierResult
        worktree = self._worktree_path()
        if not worktree:
            self._transition(ProjectState.HUMAN, "verifier",
                            "no worktree path resolvable", {})
            return
        verifier = self.verifier or FakeVerifierProvider()
        verify_id = make_id("VERIFY")
        if self.store.verification_seen(verify_id):
            return
        if self.state != ProjectState.VERIFIER_PENDING:
            if is_legal_transition(self.state, ProjectState.VERIFIER_PENDING):
                self._transition(ProjectState.VERIFIER_PENDING, "closed_loop",
                                 "gate passed -> independent verification", {})
            else:
                self._halt_budget("cannot enter verifier from %s" % self.state)
                return
        if run is None:
            run = self.gate.run(self.task, worktree)
        gate_output = "\n".join(
            "$ %s\n%s%s" % (r.get("command", ""), r.get("stdout", ""),
                            r.get("stderr", ""))
            for r in run.results) if run else ""
        # deterministic findings: trusted facts from our own path gate
        findings = []
        forbidden, outside = self._path_violations()
        for v in forbidden:
            findings.append("path violation (forbidden): %s" % v)
        for v in outside:
            findings.append("path violation (outside allowed): %s" % v)
        changed = wt.changed_paths(worktree, self._base_commit())
        if changed is None:
            # 簇七(HIGH): git inspection failed — the change set is UNKNOWN.
            # Iterating None crashed the whole runner (verified); verifying
            # against unknown evidence would be worse. Fail closed to HUMAN
            # (mirrors mission.py's final-gate None handling).
            self._transition(ProjectState.HUMAN, "verifier",
                             "verification evidence unavailable: git error",
                             {})
            return
        tests_touched = [p for p in changed
                         if p.replace("\\", "/").startswith("tests/")]
        if tests_touched:
            findings.append("tests/ files changed by worker: %s"
                            % ", ".join(tests_touched))
        if run is not None and getattr(run, "head_mutated", False):
            # the gate itself moved HEAD (a committing test, a rewriting
            # hook): every 'diff vs frozen base' fact is now questionable.
            findings.append("gate execution mutated HEAD: %s -> %s"
                            % (run.head_before, run.head_after))
        inp = VerifierInput(
            task_spec=self.task.to_dict(),
            diff=self._git_diff(),
            gate_output=gate_output,
            changed_paths=changed,
            deterministic_findings=findings,
            user_notes=self.role_directives["verifier"][-10:])
        result = verifier.verify(inp, verify_id)
        self.store.record_verification(verify_id, self.task.task_id,
                                       result.to_dict())
        # --- verdict coherence cross-check (review: verifier evidence) ---
        ac_fails = [c for c in result.ac_checks if c.verdict == "FAIL"]
        if result.verdict == "FAIL" and not ac_fails \
                and not result.anti_gaming:
            # FAIL with every AC green and zero anti-gaming flags smells
            # like malformed verifier output, not a real rejection. Retry
            # ONCE with a fresh id; a second incoherent FAIL goes to HUMAN
            # (bounded — never re-enters audit with empty evidence).
            retry_id = make_id("VERIFY-RETRY")
            if not self.store.verification_seen(retry_id):
                second = verifier.verify(inp, retry_id)
                self.store.record_verification(retry_id, self.task.task_id,
                                               second.to_dict())
                second_bad = [c for c in second.ac_checks
                              if c.verdict == "FAIL"]
                if second.verdict == "FAIL" and not second_bad \
                        and not second.anti_gaming:
                    self._transition(ProjectState.HUMAN, "verifier",
                        "verifier returned incoherent FAIL twice "
                        "(all AC checks green, no anti-gaming flags)",
                        {"verify_id": retry_id})
                    return
                result, ac_fails = second, second_bad
        if result.verdict == "PASS" and ac_fails:
            # incoherent PASS: a verdict contradicting its own per-AC checks
            # must never reach DONE — downgrade to FAIL and let the audit
            # loop route the corrective fix.
            result.verdict = "FAIL"
            result.summary = ("[downgraded] PASS contradicted by failing AC "
                              "checks (%s); original: %s"
                              % (", ".join(c.ac_id for c in ac_fails),
                                 result.summary))
            self.store.record_verification(verify_id + "-downgraded",
                                           self.task.task_id,
                                           result.to_dict())
        if result.verdict == "PASS":
            if is_legal_transition(self.state, ProjectState.DONE):
                self._transition(ProjectState.DONE, "verifier",
                                 "verifier PASS: %s" % result.summary[:200],
                                 {"verify_id": verify_id,
                                  "ac_checks": [c.to_dict()
                                                for c in result.ac_checks]})
        else:
            # FAIL: verifier findings become Auditor evidence; the Planner
            # decides the corrective route (bounded by budgets).
            if is_legal_transition(self.state, ProjectState.AUDIT_PENDING):
                self._transition(ProjectState.AUDIT_PENDING, "verifier",
                                 "verifier FAIL: %s" % result.summary[:200],
                                 {"verify_id": verify_id})
                audit_id = make_id("AUDIT-VERIF")
                if not self.store.audit_seen(audit_id):
                    from .mission_contracts import AuditEvidence
                    ev = [AuditEvidence(
                        type="verifier_fail", summary=c.note or c.ac_id,
                        reference="ac_check %s=%s" % (c.ac_id, c.verdict))
                        for c in (result.ac_checks + result.anti_gaming)
                        if c.verdict == "FAIL"]
                    if not ev:
                        ev = [AuditEvidence(
                            type="verifier_fail",
                            summary=result.summary or "verifier FAIL",
                            reference=verify_id)]
                    bundle = EvidenceBundle(
                        task_spec=self.task.to_dict(), alert=None,
                        worker_id=self.task.worker_session_id,
                        subtask_id=getattr(self.task, "subtask_of", None),
                        worker_status=self._worker_status(),
                        git_diff=self._git_diff(),
                        failed_criteria=result.failed_acs() or
                                        [ac.id for ac in
                                         self.task.acceptance_criteria],
                        test_output=gate_output,
                        history={"local_fixes": self.executor.local_fixes,
                                 "replans": self.executor.replans,
                                 "user_directives":
                                     self.role_directives["auditor"][-10:]})
                    audit = self.auditor.audit(bundle, audit_id)
                    self.store.record_audit(audit_id, self.task.task_id,
                                            audit.to_dict())
                    self._to_planner(audit)

    def _completion_audit(self) -> None:
        """COMPLETION_AUDIT: worker went idle after a fix/run.

        Runs the acceptance commands, captures real test output, and lets the
        Auditor decide the next move. PASS routes through the Planner
        (CANDIDATE_DONE) to the gate; LOCAL_FIX/REPLAN/HUMAN re-enter the loop.
        This is the second audit mode beside the alert-driven ALERT audit.
        """
        audit_id = make_id("AUDIT-COMPL")
        if self.store.audit_seen(audit_id):
            return
        if self.state != ProjectState.AUDIT_PENDING:
            if is_legal_transition(self.state, ProjectState.AUDIT_PENDING):
                self._transition(ProjectState.AUDIT_PENDING, "closed_loop",
                                 "worker idle -> completion audit", {})
            else:
                self._halt_budget("cannot enter audit from %s" % self.state)
                return
        run, test_output = self._run_gate_capture()
        satisfied, failed = self._ac_from_gate(run)
        bundle = EvidenceBundle(
            task_spec=self.task.to_dict(), alert=None, alerts=[],
            worker_id=self.task.worker_session_id,
            subtask_id=getattr(self.task, "subtask_of", None),
            events=[], worker_status=self._worker_status(),
            git_diff=self._git_diff(),
            test_output=test_output,
            satisfied_criteria=satisfied,
            failed_criteria=failed,
            history={"local_fixes": self.executor.local_fixes,
                     "replans": self.executor.replans,
                     "user_directives": self.role_directives["auditor"][-10:]},
            audit_type="COMPLETION")
        audit = self.auditor.audit(bundle, audit_id)
        self.store.record_audit(audit_id, self.task.task_id, audit.to_dict())
        self._to_planner(audit)

    def _worktree_path(self) -> Optional[str]:
        # AO lays out worker worktrees at <AO_DATA_DIR>/worktrees/<project>/<session>
        # (verified on disk). Fall back to the older data/worktrees layout.
        import os
        data_dir = os.environ.get("AO_DATA_DIR", "")
        if not data_dir:
            return None
        cand = Path(data_dir) / "worktrees" / self.task.project_id / \
            (self.task.worker_session_id or "")
        if cand.exists():
            return str(cand)
        old = Path(data_dir) / "data" / "worktrees" / self.task.project_id / \
            (self.task.worker_session_id or "")
        return str(old) if old.exists() else str(cand)

    def _path_violations(self):
        """(forbidden_violations, allowed_violations) via worktree.py.

        Covers the FULL changed-path set relative to the frozen base commit —
        staged + committed + untracked + renamed + deleted files, not just
        `git diff --name-only` (which previously missed untracked files and
        let a worker edit tests by `git add`-ing then reverting).

        Fail-closed: git errors or an unfrozen base produce a sentinel
        violation so the gate halts to HUMAN instead of waving an
        unauditable tree through (review 簇四).
        """
        worktree = self._worktree_path()
        if not worktree:
            return [], []
        base = self._base_commit()
        if not base:
            return ["<base-commit unknown: cannot audit paths>"], []
        try:
            return wt.path_violations(
                worktree, base,
                allowed_paths=list(self.task.allowed_paths or []),
                forbidden_paths=list(self.task.forbidden_paths or []))
        except Exception:
            return ["<path-gate error: git inspection failed>"], []

    def _forbidden_violations(self) -> List[str]:
        """Back-compat shim: changed paths matching TaskSpec.forbidden_paths."""
        return self._path_violations()[0]
