"""Mission layer tests: decompose -> dispatch N workers -> merge -> DONE.

Fakes only; temp SQLite; no real AO/Claude/git-remote. Verifies the core
promise: ONE user instruction drives multiple workers through their own
closed loops to a merged, verified MISSION_DONE — with budgets halting
runaways.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from loopcore.action_executor import ActionExecutor, ActionResult
from loopcore.auditor import FakeAuditorProvider
from loopcore.mission_contracts import (MissionSpec, PlannerAction, PlannerActionType,
                           ProjectState)
from loopcore.mission_gate import IntegrationGate
from loopcore.mission import MissionController
from loopcore.event_observer import Observer
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider
from tests.sidecar_port.test_budgets import _cfg


MISSION = {
    "mission_id": "MIS-TEST-1",
    "project_id": "closed-loop-demo",
    "objective": "实现 divide 和 multiply 两个函数",
    "allowed_paths": ["app.py", "math2.py"],
    "forbidden_paths": ["tests/**", ".git/**"],
    "acceptance_criteria": [
        {"id": "AC-01", "description": "divide(6,3)==2"},
        {"id": "AC-02", "description": "multiply(2,3)==6"},
    ],
    "gate_commands": ["python -m pytest -q"],
    "user_instruction": "测试全绿即完成；禁止改 tests/",
    "budgets": {"max_subtasks": 3, "max_total_replans": 2,
                "max_runtime_seconds": 1800},
}


def _mc(tmp_path, *, dry=False):
    store = StateStore(tmp_path / "m.db")
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": "w", "status": "idle",
                                              "activity": {"state": "idle"}}
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    mc = MissionController(
        mission=MissionSpec.from_dict(MISSION), cfg=_cfg(),
        planner=FakePlannerProvider(), auditor=FakeAuditorProvider(),
        verifier=FakeVerifierProvider(), executor=ex, adapter=adapter,
        gate=gate, store=store, dry_run=dry)
    return mc, store


def test_decompose_creates_two_subtask_loops(tmp_path):
    mc, store = _mc(tmp_path)
    r = mc.step()      # first step: decomposition
    assert r["acted"]
    assert mc.plan is not None
    assert len(mc.plan.subtasks) == 2
    assert set(mc.tasks) == {s.subtask_id for s in mc.plan.subtasks}
    # each subtask spec records subtask_of (attribution)
    for t in mc.tasks.values():
        assert t.subtask_of == "MIS-TEST-1"
    # each loop carries the user instruction (leader absorbs it)
    for loop in mc.loops.values():
        assert loop.instruct == "测试全绿即完成；禁止改 tests/"
        assert callable(loop.board)


def test_dispatch_respects_dependencies(tmp_path):
    """Fake decompose makes S2 depend on S1: only S1 spawns first."""
    mc, store = _mc(tmp_path)
    mc.step()          # decompose
    spawned = {}
    def fake_spawn(task):
        sid = "sess-" + task.task_id[-2:]
        spawned[task.task_id] = sid
        return sid
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()      # dispatch
    s1 = [s for s in mc.plan.subtasks if not s.dependencies][0].subtask_id
    s2 = [s for s in mc.plan.subtasks if s.dependencies][0].subtask_id
    assert s1 in spawned
    assert s2 not in spawned          # dep not DONE yet -> held
    # mark S1 DONE -> S2 becomes dispatchable on the next step
    store.record_transition(task_id=s1, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()
    assert s2 in spawned


def test_full_mission_to_done_with_merge(tmp_path):
    """End-to-end fake run: 2 subtasks DONE -> merge -> final verify PASS."""
    import subprocess
    import os
    # real mini git repo simulating the AO worktree layout
    data_dir = tmp_path / "ao-data"
    proj = data_dir / "worktrees" / "closed-loop-demo"
    proj.mkdir(parents=True)
    def _git(cwd, *a):
        subprocess.run(["git", "-C", str(cwd), *a], capture_output=True)
    _git(proj, "init", "-q")
    _git(proj, "config", "user.name", "t")
    _git(proj, "config", "user.email", "t@t")
    (proj / "app.py").write_text("x=1\n", encoding="utf-8")
    _git(proj, "add", "-A"); _git(proj, "commit", "-q", "-m", "init")
    # worker worktrees (as AO would create per session)
    wts = {}
    for name in ("sess-S1", "sess-S2"):
        p = proj / name
        subprocess.run(["git", "-C", str(proj), "worktree", "add", "-q",
                        "-b", name, str(p)], capture_output=True)
        wts[name] = p
    (wts["sess-S1"] / "app.py").write_text("def divide(a,b):\n"
        "    if b==0: raise ValueError\n    return a/b\n", encoding="utf-8")
    (wts["sess-S2"] / "math2.py").write_text(
        "def multiply(a,b): return a*b\n", encoding="utf-8")
    os.environ["AO_DATA_DIR"] = str(data_dir)

    mc, store = _mc(tmp_path)
    mc.step()        # decompose
    spawned = {}
    def fake_spawn(task):
        sid = "sess-" + task.task_id[-2:]
        spawned[task.task_id] = sid
        return sid
    # seed both subtasks as DONE (unit test of the merge/final stage)
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()    # dispatch S1 only (S2 dep)
    s1 = [s for s in mc.plan.subtasks if not s.dependencies][0].subtask_id
    s2 = [s for s in mc.plan.subtasks if s.dependencies][0].subtask_id
    store.record_transition(task_id=s1, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()    # S2 dispatch; S1 merge happens
    assert s1 in mc.merged, "S1 merged into integration worktree"
    store.record_transition(task_id=s2, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    # final step: merge S2 + final gate + mission verifier
    gate_ok = MagicMock(ok=True, results=[
        {"command": "pytest", "stdout": "4 passed", "stderr": ""}])
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        r = mc.step()
    assert s2 in mc.merged
    assert r["state"] == "MISSION_DONE"
    # merged tree contains both subtask outputs
    integ = data_dir / "worktrees" / "closed-loop-demo" / \
        ("integration-" + mc.mission.mission_id)
    assert "divide" in (integ / "app.py").read_text(encoding="utf-8")
    assert (integ / "math2.py").exists()


def test_mission_runtime_budget_halts(tmp_path):
    mc, store = _mc(tmp_path)
    mc.step()
    store.counter_set("mission_started_at:MIS-TEST-1", 1)  # ancient start
    r = mc.step()
    assert r["state"] == "HUMAN"


def test_mission_follows_subtask_human(tmp_path):
    """All subtasks HUMAN (none dispatchable) -> mission HUMAN, watch stops."""
    mc, store = _mc(tmp_path)
    mc.step()
    for sid in mc.tasks:
        store.record_transition(task_id=sid, from_state="WORKER_RUNNING",
                                to_state="HUMAN", actor="budget",
                                reason="max_runtime_seconds exceeded",
                                evidence={})
    r = mc.step()
    assert r["state"] == "HUMAN"
    assert "halted for human" in mc._read_state().get("reason", "")


def test_mission_follows_subtask_failed(tmp_path):
    """A FAILED subtask fails the mission (cannot deliver full scope)."""
    mc, store = _mc(tmp_path)
    mc.step()
    sids = list(mc.tasks)
    store.record_transition(task_id=sids[0], from_state="WORKER_RUNNING",
                            to_state="FAILED", actor="t", reason="t",
                            evidence={})
    r = mc.step()
    assert r["state"] == "FAILED"


def test_progress_board_shape(tmp_path):
    mc, store = _mc(tmp_path)
    mc.step()
    board = mc._progress_board()
    assert board["mission_id"] == "MIS-TEST-1"
    assert len(board["subtasks"]) == 2
    assert board["user_instruction"] == "测试全绿即完成；禁止改 tests/"
    assert all("state" in s and "worker_session_id" in s
               for s in board["subtasks"])


def test_dry_run_never_spawns(tmp_path):
    mc, store = _mc(tmp_path, dry=True)
    with patch.object(mc.executor, "spawn_initial_worker") as sp:
        mc.step()   # decompose
        mc.step()   # would dispatch
        sp.assert_not_called()
