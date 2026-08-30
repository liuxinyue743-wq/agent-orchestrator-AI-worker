"""Two mission-level guard fixes:

1. max_total_replans: the mission-wide spawn budget is enforced in
   ActionExecutor._replan_spawn — every subtask shares ONE counter.
2. Terminal cleanup: MissionController._set_state entering a terminal
   state kills every still-bound live worker (no orphan workers running
   against a halted mission — real-run: PANEL-203226 S2 finished `half()`
   into a mission that had already halted on a merge conflict).
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock, patch

from loopcore.action_executor import ActionExecutor, ActionResult
from loopcore.mission import MissionController, MISSION_TERMINAL
from loopcore.mission_contracts import (MissionSpec, PlannerAction,
                                        PlannerActionType, TaskSpec,
                                        ProjectState)
from loopcore.state_store import StateStore


def _store(tmp_path):
    return StateStore(str(tmp_path / "s.db"))


def _task(subtask_of=None, max_replans=1):
    return TaskSpec(
        task_id="T-R1", project_id="p", objective="obj",
        allowed_paths=["a.py"], forbidden_paths=[],
        acceptance_criteria=[], gate_commands=["python -m pytest -q"],
        worker_session_id="w1", subtask_of=subtask_of,
        budgets={"max_local_fixes": 2, "max_replans": max_replans,
                 "max_same_alerts": 2, "max_runtime_seconds": 1800})


def _mission_row(store, mission_id, max_total):
    store.record_mission(mission_id, {
        "state": "MISSION_RUNNING", "reason": "",
        "mission": {"mission_id": mission_id, "project_id": "p",
                    "objective": "o", "allowed_paths": [], "forbidden_paths": [],
                    "acceptance_criteria": [], "gate_commands": [],
                    "budgets": {"max_total_replans": max_total}},
        "plan": None})


def _replan_action():
    return PlannerAction(action_id="A-R1", task_id="T-R1",
                         action=PlannerActionType.REPLAN_SPAWN,
                         reason="test", target_session_id="w1")


def test_total_replans_enforced(tmp_path):
    store = _store(tmp_path)
    _mission_row(store, "M-1", 2)
    ex = ActionExecutor("ao", "d", "r", store)
    task = _task(subtask_of="M-1", max_replans=5)
    # subtask budget (5) allows more, but mission budget is 2
    with patch.object(ex, "_spawn", return_value="new-w"), \
            patch.object(ex, "_run", return_value=MagicMock(returncode=0)):
        r1 = ex._replan_spawn(_replan_action(), task)   # uses mission slot 1
        r2 = ex._replan_spawn(_replan_action(), task)   # uses mission slot 2
        assert r1.ok and r2.ok
        r3 = ex._replan_spawn(_replan_action(), task)   # over budget
    assert not r3.ok
    assert "max_total_replans" in r3.detail
    assert r3.new_state == ProjectState.HUMAN


def test_total_replans_ignored_without_mission_budget(tmp_path):
    """No mission row / zero budget -> old per-task behaviour only."""
    store = _store(tmp_path)
    ex = ActionExecutor("ao", "d", "r", store)
    task = _task(subtask_of=None)   # standalone task: no parent mission
    with patch.object(ex, "_spawn", return_value="new-w"), \
            patch.object(ex, "_run", return_value=MagicMock(returncode=0)):
        r = ex._replan_spawn(_replan_action(), task)
    assert r.ok


def _controller(tmp_path, workers):
    store = _store(tmp_path)
    store.record_mission("M-2", {"state": "MISSION_RUNNING", "reason": "",
                                 "mission": {}, "plan": None})
    mc = MissionController.__new__(MissionController)
    mc.mission = MagicMock()
    mc.mission.mission_id = "M-2"
    mc.mission.to_dict.return_value = {"mission_id": "M-2"}
    mc.store = store
    mc.dry_run = False
    mc.plan = None
    mc.tasks = {sid: MagicMock(worker_session_id=ws)
                for sid, ws in workers.items()}
    mc.executor = MagicMock()
    return mc


def test_terminal_state_kills_workers(tmp_path):
    live = {"S1": "worker-a", "S2": "worker-b"}
    mc = _controller(tmp_path, live)
    mc._set_state("HUMAN", "merge conflict")
    killed = {c.args[0] for c in mc.executor.kill_worker.call_args_list}
    assert killed == {"worker-a", "worker-b"}


def test_non_terminal_state_kills_nothing(tmp_path):
    mc = _controller(tmp_path, {"S1": "worker-a"})
    mc._set_state("MISSION_RUNNING", "tick")
    assert mc.executor.kill_worker.call_count == 0


def test_re_entering_same_terminal_kills_once(tmp_path):
    """Idempotency: re-setting the SAME terminal state must not re-kill
    (e.g. a watchdog writing HUMAN twice)."""
    mc = _controller(tmp_path, {"S1": "worker-a"})
    mc._set_state("HUMAN", "first")
    mc._set_state("HUMAN", "again")   # prev == s -> no cleanup re-run
    assert mc.executor.kill_worker.call_count == 1
