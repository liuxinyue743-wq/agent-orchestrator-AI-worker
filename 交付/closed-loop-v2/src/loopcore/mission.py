"""Mission-level orchestration: ONE user instruction -> fully automatic run.

Architecture (the leader sits in the Planner, per the V0.1 audit docs —
there is deliberately NO coordinator agent):

    user --one instruction--> MissionSpec
       |
    Planner.plan_decompose()          (once; 2..max_subtasks subtasks,
                                       exactly 1 when max_subtasks=1)
       |
    N x ClosedLoop (one per subtask, each with its own ao-spawned worker,
                    per-worker frozen base, budgets, audit->planner loop,
                    gate -> independent Verifier -> DONE)
       |
    integration merge (trusted code: commit + fetch + merge per subtask)
       |
    final gate + mission-level Verifier on the merged tree
       |
    MISSION_DONE / HUMAN (only human touchpoint)

The MissionController polls the project event stream ONCE per tick and routes
items to each subtask loop by session id (avoids N x API calls). Workers run
in parallel server-side (AO); the controller is single-threaded.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import worktree as wt
from .action_executor import ActionExecutor
from .ao_adapter import AOAdapter
from .auditor import AuditorProvider
from .closed_loop import ClosedLoop
from .mission_contracts import (MissionPlan, MissionSpec, ProjectState, TaskSpec)
from .mission_gate import IntegrationGate
from .event_observer import Observer
from .planner_adapter import PlannerProvider
from .state_store import StateStore
from .verifier import VerifierInput, VerifierProvider
from .event_normalizer import now_iso, make_id, _epoch_seconds

MISSION_TERMINAL = ("MISSION_DONE", "HUMAN", "FAILED")


class MissionController:
    def __init__(self, mission: MissionSpec, cfg: Dict, *,
                 planner: PlannerProvider,
                 auditor: AuditorProvider,
                 verifier: VerifierProvider,
                 executor: ActionExecutor,
                 adapter: AOAdapter,
                 gate: IntegrationGate,
                 store: StateStore,
                 dry_run: bool = False):
        self.mission = mission
        self.cfg = cfg
        self.planner = planner
        self.auditor = auditor
        self.verifier = verifier
        self.executor = executor
        self.adapter = adapter
        self.gate = gate
        self.store = store
        self.dry_run = dry_run
        self.plan: Optional[MissionPlan] = None
        self.tasks: Dict[str, TaskSpec] = {}        # subtask_id -> TaskSpec
        self.loops: Dict[str, ClosedLoop] = {}      # subtask_id -> loop
        self.merged: List[str] = []                 # subtask ids merged
        self._shared_observer = Observer(cfg, state_store=store)
        # user directives posted mid-mission (web panel / operator UI);
        # drained once per tick and routed to each target's real input.
        from .directives import DirectiveChannel
        self.directives = DirectiveChannel()
        # User-stop latch (panel /api/stop): set by request_stop(); every
        # controller checkpoint and every subtask loop (shared via
        # _build_loop) refuses to act once raised.
        self._stop_event = threading.Event()

    # ------------------------------------------------------------- state
    @property
    def state(self) -> str:
        return self._read_state().get("state", "MISSION_READY")

    def _set_state(self, s: str, reason: str) -> None:
        """Mission state lives in the store (per-DB, not a shared file) so
        parallel/renamed stores never cross-contaminate.

        The terminal-state check and the write happen atomically inside the
        store lock (record_mission_state_atomic), so a mission thread writing
        MISSION_DONE and a panel thread writing HUMAN via request_stop() cannot
        both pass the check and have the loser clobber the winner's terminal
        state. The first terminal write wins."""
        prev = self.state
        # Local pre-check keeps the common (non-racing) path cheap and lets us
        # skip worker cleanup when no transition actually occurs; the store's
        # atomic method is the source of truth under contention.
        if prev in MISSION_TERMINAL and s != prev:
            return
        self._mission_row = {"state": s, "reason": reason, "at": now_iso()}
        # Only carry the plan when we hold one: with store-level merging, an
        # explicit "plan": null would erase a previously recorded plan.
        payload = {"reason": reason, "at": now_iso(),
                   "mission": self.mission.to_dict()}
        if self.plan is not None:
            payload["plan"] = self.plan.to_dict()
        merged = getattr(self, "merged", None)
        if merged:
            payload["merged"] = list(merged)
        landed = self.store.record_mission_state_atomic(
            self.mission.mission_id, s, payload)
        if not landed:
            # A concurrent terminal write won; do not run terminal cleanup for
            # a transition that did not land.
            self._mission_row = None  # force re-read from store next time
            return
        # Terminal transition: stop every still-bound live worker. A mission
        # that halts (merge conflict, FAILED subtask, budget) must not leave
        # orphan workers running against a mission nobody will merge — their
        # output would land in worktrees no controller ever reads again
        # (real-run: MISSION-PANEL-203226 S2 completed `half()` into a void).
        if s in MISSION_TERMINAL and prev != s and not self.dry_run:
            # Snapshot tasks before iterating: kill_worker shells out (releases
            # the GIL), and a concurrent _decompose on the mission thread could
            # otherwise insert a key mid-iteration -> "dict changed size during
            # iteration". request_stop() can reach here from a panel thread.
            for sid, task in list(self.tasks.items()):
                if task.worker_session_id:
                    try:
                        self.executor.kill_worker(task.worker_session_id)
                    except Exception:
                        pass

    def _read_state(self) -> Dict:
        row = getattr(self, "_mission_row", None)
        if row:
            return row
        try:
            with self.store._lock:
                cur = self.store._conn.execute(
                    "SELECT payload_json FROM missions WHERE mission_id=?",
                    (self.mission.mission_id,))
                r = cur.fetchone()
            if r:
                return json.loads(r[0])
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------- stop
    def request_stop(self) -> None:
        """User-initiated stop (panel /api/stop). Lands the mission in HUMAN
        immediately — the terminal transition inside _set_state reaps every
        still-bound worker — and raises the shared stop event so every
        controller checkpoint and subtask loop refuses further work.
        Idempotent; a mission already terminal is left untouched."""
        self._stop_event.set()
        if self.state not in MISSION_TERMINAL:
            self._set_state("HUMAN", "stopped by user")

    # ------------------------------------------------------------- step
    # Consecutive mission-tick exceptions tolerated before halting to HUMAN
    # (mirrors ClosedLoop.MAX_CONSECUTIVE_LOOP_ERRORS; an unbounded retry
    # loop here re-dispatches subtasks and burns model calls every tick).
    MAX_CONSECUTIVE_LOOP_ERRORS = 3

    def step(self) -> Dict:
        """Top-level boundary (簇五): an unexpected error inside a tick must
        be recorded and returned, never propagated into the runner's while
        loop where it would kill the whole mission process.

        Bounded retries: after MAX_CONSECUTIVE_LOOP_ERRORS consecutive
        failures the mission halts to HUMAN instead of retrying forever (a
        successful tick resets the streak)."""
        try:
            result = self._step_impl()
            self._loop_error_streak = 0
            return result
        except Exception as e:
            self._loop_error_streak = getattr(self, "_loop_error_streak", 0) + 1
            try:
                self.store.record_alert(
                    "LOOPERR-MISSION-%s-%d" % (self.mission.mission_id,
                                               _epoch_seconds()),
                    {"alert_type": "LOOP_ERROR",
                     "mission_id": self.mission.mission_id,
                     "state": self.state, "error": str(e)[:500],
                     "consecutive": self._loop_error_streak})
            except Exception:
                pass
            if self._loop_error_streak >= self.MAX_CONSECUTIVE_LOOP_ERRORS:
                # Persistent fault: stop the retry loop; a human must look.
                self._loop_error_streak = 0
                try:
                    self._set_state("HUMAN",
                                    "consecutive loop errors (%d): %s: %s"
                                    % (self.MAX_CONSECUTIVE_LOOP_ERRORS,
                                       type(e).__name__, str(e)[:200]))
                except Exception:
                    pass
                return {"state": "HUMAN", "acted": True,
                        "error": "%s: %s" % (type(e).__name__, e)}
            return {"state": self.state, "acted": False,
                    "error": "%s: %s" % (type(e).__name__, e)}

    def _step_impl(self) -> Dict:
        result = {"state": self.state, "acted": False}
        if self.state in MISSION_TERMINAL:
            return result
        # User stop: act no further (request_stop already landed HUMAN and
        # reaped the workers; an in-flight tick just unwinds from here).
        if self._stop_event.is_set():
            return result
        # mission runtime watchdog
        if self._runtime_exceeded():
            self._set_state("HUMAN", "mission max_runtime_seconds exceeded")
            result["state"] = "HUMAN"
            return result
        if self.plan is None:
            # restart recovery: a previous process may have already
            # decomposed (plan + tasks live in the store) — rehydrate
            # instead of re-decomposing (fresh subtask_ids would orphan
            # already-dispatched workers).
            if not self._hydrate():
                result["acted"] = True
                self._decompose()
            result["state"] = self.state
            return result
        # collect the project event stream ONCE, route per subtask
        self._collect_all_events()
        self._apply_directives()
        for sid, task in list(self.tasks.items()):
            if self._stop_event.is_set():
                result["state"] = self.state
                return result
            loop = self.loops.get(sid)
            if loop is None:
                continue
            if task.worker_session_id:
                evs = self._route_events(loop, task.worker_session_id)
            else:
                evs = []
            loop.step(injected_events=evs)
        if self._stop_event.is_set():
            result["state"] = self.state
            return result
        # dispatch newly-ready subtasks (deps satisfied)
        self._dispatch_ready()
        if self._stop_event.is_set():
            result["state"] = self.state
            return result
        # merge finished subtasks into the integration worktree
        self._merge_done()
        # any subtask terminally FAILED -> the mission cannot deliver its
        # scope; escalate (watch loop exits instead of spinning forever)
        failed = [sid for sid in self.tasks
                  if self._subtask_state(sid) == ProjectState.FAILED]
        if failed:
            result["acted"] = True
            self._set_state("FAILED",
                            "subtask(s) FAILED: %s" % ", ".join(failed))
        # every subtask reached HUMAN and none can make progress -> mission
        # escalates too (a HUMAN subtask never self-recovers)
        elif all(self._subtask_state(sid) in (ProjectState.HUMAN,
                                              ProjectState.DONE)
                 for sid in self.tasks) \
                and any(self._subtask_state(sid) == ProjectState.HUMAN
                        for sid in self.tasks) \
                and not self._dispatchable():
            result["acted"] = True
            human = [sid for sid in self.tasks
                     if self._subtask_state(sid) == ProjectState.HUMAN]
            self._set_state("HUMAN",
                            "subtask(s) halted for human: %s" % ", ".join(human))
        # all subtasks DONE (+ merged) -> final gate + mission verifier
        elif not self._stop_event.is_set() \
                and self._all_done() and len(self.merged) == len(self.tasks):
            result["acted"] = True
            self._final_verify()
        result["state"] = self.state
        # refresh derived state
        st = self._read_state().get("state", "MISSION_READY")
        result["state"] = st
        return result

    # ------------------------------------------------------- decomposition
    def _hydrate(self) -> bool:
        """Rebuild plan/tasks/loops from the store after a process restart.
        Returns True when a plan was found (skip decomposition). A corrupted
        mission row is escalated to HUMAN (not re-decomposed) — fresh subtask_ids
        would orphan already-dispatched workers."""
        with self.store._lock:
            cur = self.store._conn.execute(
                "SELECT payload_json FROM missions WHERE mission_id=?",
                (self.mission.mission_id,))
            r = cur.fetchone()
        if not r:
            return False
        try:
            d = json.loads(r[0])
        except (ValueError, TypeError):
            # A partially-written/corrupted mission row (crash mid-write; WAL
            # lowers but does not eliminate this). Re-decomposing would mint
            # new subtask_ids and orphan workers already running under the old
            # ids -> fail closed to HUMAN instead.
            self._set_state("HUMAN",
                            "mission row corrupted on resume; manual review "
                            "required (re-decompose would orphan workers)")
            return True
        try:
            plan_d = d.get("plan")
            if not plan_d or not plan_d.get("subtasks"):
                return False
            self.plan = MissionPlan.from_dict(plan_d)
            self._mission_row = d
            # Restore the merged-subtask list so _all_done+merged can re-fire
            # final verify after a crash. Without this, merged==[] on resume;
            # if a subtask's worktree was since cleaned up, _merge_done skips it
            # forever and final verify never triggers (mission never terminates).
            restored = d.get("merged") or []
            if isinstance(restored, list):
                valid = {s.subtask_id for s in self.plan.subtasks}
                self.merged = [s for s in restored if s in valid]
        except Exception:
            self._set_state("HUMAN",
                            "mission plan unreadable on resume; manual review "
                            "required (re-decompose would orphan workers)")
            return True
        # tasks were recorded at decomposition/dispatch time; rebuild loops
        for sub in self.plan.subtasks:
            spec_d = self.store.load_task(sub.subtask_id)
            task = TaskSpec.from_dict(spec_d) if spec_d else None
            if task is None:            # never dispatched; rebuild from plan
                task = TaskSpec(
                    task_id=sub.subtask_id,
                    project_id=self.mission.project_id,
                    objective=sub.objective,
                    allowed_paths=sub.allowed_paths,
                    forbidden_paths=list(self.mission.forbidden_paths),
                    acceptance_criteria=sub.acceptance_criteria,
                    gate_commands=list(sub.gate_commands or
                                       self.mission.gate_commands),
                    dependencies=list(sub.dependencies),
                    worker_harness=self.mission.worker_harness,
                    budgets=dict(self.mission.budgets.get("subtask_budgets", {
                        "max_local_fixes": 2, "max_replans": 1,
                        "max_same_alerts": 2, "max_runtime_seconds": 1800})),
                    subtask_of=self.mission.mission_id)
            self.tasks[sub.subtask_id] = task
            self.loops[sub.subtask_id] = self._build_loop(task)
        return True

    def _build_loop(self, task: TaskSpec) -> ClosedLoop:
        loop = ClosedLoop(
            task=task, cfg=self.cfg, auditor=self.auditor,
            planner=self.planner, executor=self.executor,
            observer=self._shared_observer, adapter=self.adapter,
            gate=self.gate, store=self.store, verifier=self.verifier,
            dry_run=self.dry_run,
            instruct=self.mission.user_instruction,
            stop_event=self._stop_event)
        loop.board = self._progress_board
        loop.hold_spawn = True
        return loop

    def _decompose(self) -> None:
        try:
            plan = self.planner.plan_decompose(
                self.mission.to_dict(),
                "DECOMP-%s" % self.mission.mission_id)
        except Exception as e:  # noqa
            self._set_state("HUMAN", "decomposition failed twice: %s" % e)
            return
        # Stopped while the planner was thinking: drop the plan entirely and
        # keep the HUMAN row untouched — the next resume re-decomposes.
        if self._stop_event.is_set():
            return
        self.plan = plan
        self.store.record_mission(self.mission.mission_id, {
            "mission": self.mission.to_dict(),
            "plan": plan.to_dict()})
        # An empty decomposition (LLM returned 0 subtasks) would leave
        # self.tasks empty -> _all_done() False forever, no terminal condition
        # tripped, mission spins until the runtime watchdog (if any) kills it.
        # Fail closed to HUMAN instead of looping on nothing.
        if not plan.subtasks:
            self._set_state("HUMAN",
                            "decomposition produced 0 subtasks "
                            "(empty plan from planner)")
            return
        # materialize one TaskSpec + ClosedLoop per subtask
        for sub in plan.subtasks:
            task = TaskSpec(
                task_id=sub.subtask_id,
                project_id=self.mission.project_id,
                objective=sub.objective,
                allowed_paths=sub.allowed_paths,
                forbidden_paths=list(self.mission.forbidden_paths),
                acceptance_criteria=sub.acceptance_criteria,
                # subtask worktrees lack sibling work — use the subtask's
                # own gates when the Planner provided them; the mission-wide
                # gate runs at final verify on the merged tree instead.
                gate_commands=list(sub.gate_commands or
                                   self.mission.gate_commands),
                dependencies=list(sub.dependencies),
                worker_harness=self.mission.worker_harness,
                budgets=dict(self.mission.budgets.get("subtask_budgets", {
                    "max_local_fixes": 2, "max_replans": 1,
                    "max_same_alerts": 2, "max_runtime_seconds": 1800})),
                subtask_of=self.mission.mission_id)
            self.tasks[sub.subtask_id] = task
            self.store.record_task(task.task_id, task.to_dict())
            self.loops[sub.subtask_id] = self._build_loop(task)

    # ---------------------------------------------------------- dispatch
    def _subtask_state(self, sid: str) -> str:
        return self.store.latest_state(sid) or ProjectState.TASK_READY

    def _dispatch_ready(self) -> None:
        for sid, task in self.tasks.items():
            if task.worker_session_id:
                continue
            if self._subtask_state(sid) in (ProjectState.DONE,
                                            ProjectState.HUMAN,
                                            ProjectState.FAILED):
                continue
            deps_ok = all(
                self._subtask_state(d) == ProjectState.DONE
                for d in task.dependencies)
            if not deps_ok:
                continue
            if self.dry_run:
                continue
            # 簇五: bounded spawn retries — a task whose worker cannot be
            # spawned (daemon outage, quota 429) must not be re-attempted
            # every tick forever. After the cap the subtask goes HUMAN,
            # which escalates the mission through the normal path.
            if self.executor.spawn_cap_reached(task.task_id):
                loop = self.loops.get(sid)
                if loop is not None and self._subtask_state(sid) not in (
                        ProjectState.HUMAN, ProjectState.FAILED):
                    loop._halt_budget(
                        "initial worker spawn budget exhausted (%s)"
                        % self.executor.spawn_budget_detail(task.task_id))
                continue
            new_sid = self.executor.spawn_initial_worker(task)
            if new_sid:
                task.worker_session_id = new_sid
                self.store.record_task(task.task_id, task.to_dict())
                # freeze the per-worker diff base AT DISPATCH — before the
                # worker can commit. Freezing lazily (first gate/audit) loses
                # the race against workers that `git commit` mid-task, and
                # the verifier would then see an empty diff (real-run bug:
                # S1 implemented+committed divide yet verified as "no
                # source changes").
                worktree = (Path(os.environ.get("AO_DATA_DIR", ""))
                            / "worktrees" / self.mission.project_id / new_sid)
                if worktree.exists():
                    wt.freeze_base(str(worktree), self.store,
                                   task.task_id, scope=new_sid)

    # ------------------------------------------------------------ events
    def _apply_directives(self) -> None:
        """Drain pending user directives and route each to its target's
        real input path (see directives.py for the routing table).
        Owner-ruled visibility: non-planner directives are ALWAYS mirrored
        into the planner's instruct as well."""
        def _append_instruct(loop, line):
            # Bound the accumulated instruct so a long mission with many panel
            # directives does not grow the Planner prompt without limit (which
            # would slow every planner call and could exceed the CLI length
            # cap). Keep the most recent 20 directive lines.
            parts = (loop.instruct + "\n" + line).splitlines()
            loop.instruct = "\n".join(parts[-20:]).strip()

        for d in self.directives.drain():
            target, text = d.target, d.text
            stamp = "[用户指令 %s] %s" % (d.at[:19], text)
            if target == "planner":
                for loop in self.loops.values():
                    _append_instruct(loop, stamp)
                continue
            if target.startswith("worker:"):
                sid = target.split(":", 1)[1]
                if sid and not self.dry_run:
                    self.executor.nudge_worker(sid, stamp)
                # mirror to planner (visibility rule)
                for loop in self.loops.values():
                    _append_instruct(loop, "[镜像·发给 %s] %s" % (target, stamp))
                continue
            if target in ("auditor", "verifier"):
                for loop in self.loops.values():
                    dq = loop.role_directives[target]
                    dq.append(stamp)
                    del dq[:-20]
                    _append_instruct(loop, "[镜像·发给 %s] %s" % (target, stamp))
                continue
            # observer / gate: deterministic programs, no semantic input —
            # planner visibility only.
            for loop in self.loops.values():
                _append_instruct(loop, "[镜像·发给 %s] %s" % (target, stamp))

    def _collect_all_events(self) -> None:
        """One API call; raw items cached for per-worker routing.
        A transient daemon hiccup (restart/unresponsive window) yields an
        empty snapshot for this tick instead of crashing the mission."""
        try:
            self._last_raw_items = self.adapter.get_recent_events(
                self.mission.project_id, since=0)
        except Exception:
            self._last_raw_items = []

    def _route_events(self, loop: ClosedLoop, worker_id: str) -> List:
        """Normalize raw AO items for ONE worker using the loop's own
        normalizer (reuses ClosedLoop._collect_events filtering logic)."""
        items = getattr(self, "_last_raw_items", []) or []
        turn_times: Dict[str, Dict[str, str]] = {}
        pid = self.mission.project_id
        for item in items:
            if item["kind"] == "turn":
                t = item["turn"]
                turn_times.setdefault(item["session_id"], {})[
                    str(t.get("id"))] = t.get("requestedAt") or \
                    t.get("completedAt")
        evs = []
        for item in items:
            if item.get("session_id") != worker_id:
                if item["kind"] == "session" and \
                        item["session"].get("id") == worker_id:
                    evs += loop.normalizer().from_session(item["session"])
                continue
            if item["kind"] == "session":
                evs += loop.normalizer().from_session(item["session"])
            elif item["kind"] == "turn":
                evs += loop.normalizer().from_turn(worker_id, pid,
                                                   item["turn"])
            elif item["kind"] == "activity":
                evs += loop.normalizer().from_activity(
                    worker_id, pid, item["activity"],
                    turn_times.get(worker_id, {}), None)
        return evs

    # ------------------------------------------------------------- merge
    def _integration_wt(self) -> Optional[str]:
        data_dir = os.environ.get("AO_DATA_DIR", "")
        if not data_dir:
            return None
        base = Path(data_dir) / "worktrees" / self.mission.project_id
        # any existing worktree of the project gives us the git dir; use the
        # first task's worker worktree (workers get worktrees at spawn), else
        # the project path itself if it is a repo
        src = None
        for task in self.tasks.values():
            if task.worker_session_id:
                cand = base / task.worker_session_id
                if cand.exists():
                    src = str(cand)
                    break
        if src is None:
            src = str(Path(self.mission.project_id))
            if not (Path(src) / ".git").exists():
                return None
        integ = base / ("integration-" + self.mission.mission_id)
        out = wt.add_integration_worktree(src, "integration-%s"
                                          % self.mission.mission_id,
                                          str(integ))
        if out:
            # freeze the mission base NOW — at integration-worktree creation,
            # BEFORE any subtask merge lands — so the final mission diff shows
            # what the whole mission delivered (freezing after the merges
            # would yield an empty diff vs the merge commits themselves).
            wt.freeze_base(out, self.store, self.mission.mission_id,
                           scope="integration")
            if not self.merged:
                # Baseline failure set on the PRISTINE tree: pre-existing red
                # tests are recorded here so the final gate can separate them
                # from mission-caused failures (review 簇一).
                self._capture_baseline(out)
        return out

    # ------------------------------------------------------ baseline gate
    def _baseline_sidecar(self) -> Path:
        return Path(str(self.store.path) + ".baseline-%s.json"
                    % self.mission.mission_id)

    def _capture_baseline(self, integ: str) -> List[str]:
        """Run the mission gate commands on the pristine integration tree and
        record the failing-test id set (idempotent via a JSON sidecar).

        argv-only like the IntegrationGate (review 簇七): gate commands are
        author configuration, never handed to a shell anywhere."""
        import subprocess
        from .mission_gate import _to_argv
        from .test_failures import extract_failure_ids
        p = self._baseline_sidecar()
        if p.exists():
            try:
                return list(json.loads(p.read_text(encoding="utf-8"))
                            .get("failures", []))
            except Exception:
                return []
        failures: List[str] = []
        for cmd in self.mission.gate_commands or []:
            argv = _to_argv(cmd)
            if not argv:
                failures.append("<baseline run error: %s>" % cmd)
                continue
            try:
                proc = subprocess.run(argv, cwd=integ, shell=False,
                                      capture_output=True, text=True,
                                      timeout=300, encoding="utf-8",
                                      errors="replace")
                if proc.returncode != 0:
                    failures += extract_failure_ids(
                        (proc.stdout or "") + (proc.stderr or ""))
            except Exception:
                failures.append("<baseline run error: %s>" % cmd)
        failures = sorted(set(failures))
        try:
            p.write_text(json.dumps({"failures": failures},
                                    ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return failures

    def _baseline_failures(self) -> List[str]:
        p = self._baseline_sidecar()
        if not p.exists():
            return []  # never captured (crash window): every failure is 'new'
        try:
            return list(json.loads(p.read_text(encoding="utf-8"))
                        .get("failures", []))
        except Exception:
            return []

    def _merge_done(self) -> None:
        if self.dry_run:
            return
        for sid, task in self.tasks.items():
            if sid in self.merged:
                continue
            if self._subtask_state(sid) != ProjectState.DONE:
                continue
            if not task.worker_session_id:
                continue
            worktree = (Path(os.environ.get("AO_DATA_DIR", ""))
                        / "worktrees" / self.mission.project_id /
                        task.worker_session_id)
            if not worktree.exists():
                continue
            # Stop the worker BEFORE committing its worktree: if the AO session
            # is still alive it may write to the worktree mid-merge (cleanup,
            # cache, hooks), and commit_all would capture a half-baked state
            # into the integration branch. kill_worker is idempotent.
            try:
                self.executor.kill_worker(task.worker_session_id)
            except Exception:
                pass  # best-effort; a dead worker is fine, merge must proceed
            sha = wt.commit_all(str(worktree), "subtask %s" % sid)
            integ = self._integration_wt()
            if not integ or not sha:
                self._set_state("HUMAN",
                                "integration worktree unavailable for %s" % sid)
                return
            r = wt.merge_worktree(integ, str(worktree))
            if r.status == wt.MergeOutcome.OK:
                self.merged.append(sid)
                # Persist merged so a crash-resume can re-fire final verify
                # even if this subtask's worktree is later cleaned up.
                self.store.record_mission(self.mission.mission_id,
                                          {"merged": list(self.merged)})
            elif r.status == wt.MergeOutcome.CONFLICT:
                # deterministic conflict -> human escalation (bounded)
                self._set_state("HUMAN",
                                "merge conflict on %s: %s" % (sid,
                                                              r.detail[:200]))
                return
            else:
                self._set_state("HUMAN",
                                "merge error on %s: %s" % (sid,
                                                           r.detail[:200]))
                return

    def _all_done(self) -> bool:
        # empty tasks means decomposition hasn't materialized anything (or
        # failed) — NOT "vacuously all done"
        if not self.plan or not self.tasks:
            return False
        return all(self._subtask_state(sid) == ProjectState.DONE
                   for sid in self.tasks)

    # ------------------------------------------------------ final verify
    def _final_verify(self) -> None:
        integ = self._integration_wt()
        if not integ:
            self._set_state("HUMAN", "no integration worktree")
            return
        # mission base was frozen at integration-worktree creation (BEFORE
        # any merge) — reuse it; the final diff is the whole mission's work.
        base = wt.freeze_base(integ, self.store, self.mission.mission_id,
                              scope="integration")
        run = self.gate.run(
            TaskSpec(task_id=self.mission.mission_id,
                     project_id=self.mission.project_id,
                     objective=self.mission.objective,
                     allowed_paths=list(self.mission.allowed_paths),
                     forbidden_paths=list(self.mission.forbidden_paths),
                     acceptance_criteria=self.mission.acceptance_criteria,
                     gate_commands=list(self.mission.gate_commands)),
            integ)
        gate_output = "\n".join(
            "$ %s\n%s%s" % (r.get("command", ""), r.get("stdout", ""),
                            r.get("stderr", "")) for r in run.results)
        changed = wt.changed_paths(integ, base)
        if changed is None:
            # fail-closed: an unauditable merged tree must not reach the
            # verifier as 'clean' (review 簇四).
            self._set_state("HUMAN", "final evidence unavailable: git error "
                                     "reading the integration tree")
            return
        # Separate mission-caused failures from pre-existing (baseline) ones.
        # Legacy red tests are reported but never block MISSION_DONE; NEW
        # failures remain fatal (review 簇一).
        from .test_failures import extract_failure_ids
        current_failures = extract_failure_ids(gate_output) if not run.ok \
            else []
        baseline = set(self._baseline_failures())
        new_failures = [f for f in current_failures if f not in baseline]
        legacy_failures = [f for f in current_failures if f in baseline]
        gate_clean = run.ok or (current_failures and not new_failures)
        findings = []
        if legacy_failures:
            findings.append("pre-existing (baseline) test failures, not "
                            "caused by this mission: %s"
                            % ", ".join(legacy_failures))
        if new_failures:
            findings.append("final gate commands failed on NEW failures: %s"
                            % ", ".join(new_failures))
        elif not run.ok and not current_failures:
            findings.append("final gate commands failed")
        inp = VerifierInput(
            task_spec=self.mission.to_dict(),
            diff=wt.git_diff_text(integ, base),
            gate_output=gate_output,
            changed_paths=changed,
            deterministic_findings=findings)
        # The mission-level verify summarizes the WHOLE mission — the one
        # call that must not be lost to a transient claude/gateway hiccup
        # (real-run bug: a single subprocess failure FAILed a fully correct
        # mission). Retry "verifier invalid output" verdicts once after a
        # pause; only a genuine FAIL verdict (or a second invalid output)
        # escalates to HUMAN.
        vid = make_id("VERIFY-MISSION")
        res = self.verifier.verify(inp, vid)
        if res.verdict == "FAIL" and \
                res.summary.startswith("verifier invalid output:"):
            time.sleep(10)
            res = self.verifier.verify(
                inp, vid + "R")
        self.store.record_verification(res.verify_id, self.mission.mission_id,
                                       res.to_dict())
        if res.verdict == "PASS" and gate_clean:
            note = "final gate pass + verifier PASS"
            if legacy_failures:
                note += " (legacy baseline failures tolerated: %s)" \
                        % ", ".join(legacy_failures)
            self._set_state("MISSION_DONE", note)
        else:
            self._set_state("HUMAN",
                            "final verification failed: %s"
                            % res.summary[:200])

    def _dispatchable(self) -> bool:
        """Any not-yet-dispatched subtask whose deps are all DONE?"""
        for sid, task in self.tasks.items():
            if task.worker_session_id:
                continue
            if self._subtask_state(sid) in (ProjectState.DONE,
                                            ProjectState.HUMAN,
                                            ProjectState.FAILED):
                continue
            if all(self._subtask_state(d) == ProjectState.DONE
                   for d in task.dependencies):
                return True
        return False

    # ------------------------------------------------------------ budgets
    def _runtime_exceeded(self) -> bool:
        limit = int(self.mission.budgets.get("max_runtime_seconds", 0) or 0)
        if limit <= 0:
            return False
        key = "mission_started_at:" + self.mission.mission_id
        started = self.store.counter_get(key)
        if not started:
            self.store.counter_set(key, _epoch_seconds())
            return False
        return (_epoch_seconds() - int(started)) > limit

    # ------------------------------------------------------------ board
    def _progress_board(self) -> Dict:
        """Global view for the Planner prompt: subtasks/workers/states/
        budgets/recent audits + verifier results + last plan."""
        subs = []
        for sid, task in self.tasks.items():
            loop = self.loops.get(sid)
            subs.append({
                "subtask_id": sid,
                "worker_session_id": task.worker_session_id,
                "state": self._subtask_state(sid),
                "merged": sid in self.merged,
                "local_fixes": self.executor.local_fixes if loop else 0,
                "replans": self.executor.replans if loop else 0,
                "objective": task.objective[:200],
            })
        return {
            "mission_id": self.mission.mission_id,
            "user_instruction": self.mission.user_instruction,
            "strategy": self.plan.strategy if self.plan else "",
            "subtasks": subs,
            "merged_count": len(self.merged),
        }
