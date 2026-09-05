"""Mission layer tests: decompose -> dispatch N workers -> merge -> DONE.

Fakes only; temp SQLite; no real AO/model/git-remote. Verifies the core
promise: ONE user instruction drives multiple workers through their own
closed loops to a merged, final-verified MISSION_DONE — with budgets halting
runaways.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
from tests.sidecar_port.util import ev


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
    "budgets": {"max_subtasks": 2, "max_total_replans": 2,
                "max_runtime_seconds": 1800},
}

SINGLE_MISSION = {
    **MISSION,
    "mission_id": "MIS-SINGLE-1",
    "objective": "实现 clamp01",
    "allowed_paths": ["app.py"],
    "acceptance_criteria": [
        {"id": "AC-S1", "description": "clamp01(2)==1"},
    ],
    "gate_commands": ["python -m pytest -q"],
    "budgets": {
        "max_subtasks": 1,
        "max_total_replans": 2,
        "max_runtime_seconds": 1800,
        "subtask_budgets": {
            "max_local_fixes": 1,
            "max_replans": 1,
            "max_same_alerts": 2,
            "max_runtime_seconds": 900,
        },
    },
}


class _CountingVerifier:
    def __init__(self):
        self.task_ids = []

    def verify(self, inp, verify_id):
        self.task_ids.append(inp.task_spec.get("task_id") or
                             inp.task_spec.get("mission_id", ""))
        return FakeVerifierProvider().verify(inp, verify_id)


def _mc(tmp_path, *, dry=False, verifier=None, mission_data=None,
        planner=None):
    store = StateStore(tmp_path / "m.db")
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": "w", "status": "idle",
                                              "activity": {"state": "idle"}}
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    mc = MissionController(
        mission=MissionSpec.from_dict(mission_data or MISSION), cfg=_cfg(),
        planner=planner or FakePlannerProvider(), auditor=FakeAuditorProvider(),
        verifier=verifier or FakeVerifierProvider(), executor=ex,
        adapter=adapter,
        gate=gate, store=store, dry_run=dry)
    return mc, store


def test_single_lane_plan_is_deterministic_and_complete(tmp_path):
    planner = MagicMock(wraps=FakePlannerProvider())
    mc, store = _mc(tmp_path, mission_data=SINGLE_MISSION, planner=planner)

    result = mc.step()

    assert result["acted"] is True
    planner.plan_decompose.assert_not_called()
    assert mc.plan is not None
    assert [sub.subtask_id for sub in mc.plan.subtasks] == [
        "MIS-SINGLE-1-S1"]
    sub = mc.plan.subtasks[0]
    assert sub.objective == SINGLE_MISSION["objective"]
    assert sub.allowed_paths == SINGLE_MISSION["allowed_paths"]
    assert [vars(ac) for ac in sub.acceptance_criteria] == \
        SINGLE_MISSION["acceptance_criteria"]
    assert sub.gate_commands == SINGLE_MISSION["gate_commands"]
    assert sub.dependencies == []

    task = mc.tasks[sub.subtask_id]
    assert task.forbidden_paths == SINGLE_MISSION["forbidden_paths"]
    assert task.worker_harness == SINGLE_MISSION.get(
        "worker_harness", "codex")
    assert task.budgets == SINGLE_MISSION["budgets"]["subtask_budgets"]
    assert task.subtask_of == SINGLE_MISSION["mission_id"]
    assert mc.loops[sub.subtask_id].instruct == \
        SINGLE_MISSION["user_instruction"]

    payload = json.loads(store._conn.execute(
        "SELECT payload_json FROM missions WHERE mission_id=?",
        (SINGLE_MISSION["mission_id"],)).fetchone()[0])
    assert payload["plan"] == mc.plan.to_dict()


def test_new_mission_rejects_more_than_two_without_planner(tmp_path):
    mission = dict(
        MISSION,
        mission_id="MIS-NEW-THREE",
        budgets={"max_subtasks": 3, "max_runtime_seconds": 1800},
    )
    planner = MagicMock(wraps=FakePlannerProvider())
    mc, _store = _mc(tmp_path, mission_data=mission, planner=planner)

    result = mc.step()

    assert result["state"] == "HUMAN"
    assert "must be 1 or 2" in mc._read_state()["reason"]
    assert mc.plan is None
    planner.plan_decompose.assert_not_called()


@pytest.mark.parametrize("task_count", [2, 3])
def test_historical_plan_resumes_without_new_mission_limit(tmp_path,
                                                           task_count):
    mission = dict(
        MISSION,
        mission_id="MIS-HISTORICAL-%d" % task_count,
        budgets={"max_subtasks": task_count, "max_runtime_seconds": 1800},
    )
    planner = MagicMock(wraps=FakePlannerProvider())
    mc, store = _mc(tmp_path, mission_data=mission, planner=planner)
    plan = {
        "mission_id": mission["mission_id"],
        "strategy": "historical three-lane plan",
        "subtasks": [
            {
                "subtask_id": "%s-S%d" % (mission["mission_id"], i),
                "objective": "historical part %d" % i,
                "allowed_paths": ["part%d.py" % i],
                "acceptance_criteria": [
                    {"id": "AC-%d" % i, "description": "part works"}],
                "gate_commands": ["python -m pytest -q"],
                "dependencies": [],
            }
            for i in range(1, task_count + 1)
        ],
    }
    store.record_mission(mission["mission_id"], {
        "state": "MISSION_RUNNING",
        "mission": mission,
        "plan": plan,
    })

    result = mc.step()

    assert result["state"] == "MISSION_RUNNING"
    assert len(mc.plan.subtasks) == task_count
    assert len(mc.tasks) == task_count
    planner.plan_decompose.assert_not_called()


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
    mc.adapter.get_session_workspace.return_value = str(tmp_path)
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn), \
            patch("loopcore.mission.wt.freeze_base", return_value="BASE") as freeze:
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
                      side_effect=fake_spawn), \
            patch("loopcore.mission.wt.freeze_base", return_value="BASE"):
        mc.step()
    assert s2 in spawned
    assert freeze.call_args.args[0] == str(tmp_path)
    assert mc.adapter.get_session_workspace.call_args_list[0].args == (
        spawned[s1],)


def test_dispatch_workspace_failure_halts_mission(tmp_path):
    from loopcore.ao_adapter import AOError

    mc, store = _mc(tmp_path)
    mc.step()
    mc.adapter.get_session_workspace.side_effect = AOError(
        "SESSION_WORKSPACE_NOT_FOUND")

    with patch.object(mc.executor, "spawn_initial_worker",
                      return_value="sess-missing"), \
            patch.object(mc.executor, "kill_worker") as kill, \
            patch("loopcore.mission.wt.freeze_base") as freeze:
        result = mc.step()

    assert result["state"] == "HUMAN"
    assert any(t.worker_session_id == "sess-missing"
               for t in mc.tasks.values())
    mc.adapter.get_session_workspace.assert_called_once_with("sess-missing")
    freeze.assert_not_called()
    kill.assert_called_once_with("sess-missing")


def _bind_done_worker(mc, store, session_id="sess-done"):
    sid = next(iter(mc.tasks))
    task = mc.tasks[sid]
    task.worker_session_id = session_id
    store.record_task(task.task_id, task.to_dict())
    store.record_transition(task_id=sid, from_state="TASK_READY",
                            to_state="WORKER_RUNNING", actor="t", reason="t",
                            evidence={})
    store.record_transition(task_id=sid, from_state="WORKER_RUNNING",
                            to_state="DONE", actor="t", reason="t",
                            evidence={})
    return sid, task


def test_merge_resolves_workspace_before_kill_and_uses_actual_path(tmp_path):
    mc, store = _mc(tmp_path)
    mc.step()
    sid, task = _bind_done_worker(mc, store)
    worker = tmp_path / "actual-worker"
    worker.mkdir()
    integration = tmp_path / "integration"
    order = []

    def workspace(session_id):
        order.append(("workspace", session_id))
        return str(worker)

    def kill(session_id):
        order.append(("kill", session_id))

    def commit(path, _message):
        order.append(("commit", path))
        return "abc123"

    def integration_wt(*, source_worktree=None):
        order.append(("integration", source_worktree))
        return str(integration)

    def merge(target, source):
        order.append(("merge", target, source))
        return MagicMock(status="ok")

    mc.adapter.get_session_workspace.side_effect = workspace
    mc.executor.kill_worker = kill
    with patch("loopcore.mission.wt.commit_all", side_effect=commit), \
            patch.object(mc, "_integration_wt",
                         side_effect=integration_wt), \
            patch("loopcore.mission.wt.merge_worktree", side_effect=merge):
        mc._merge_done()

    assert order == [
        ("workspace", task.worker_session_id),
        ("kill", task.worker_session_id),
        ("commit", str(worker)),
        ("integration", str(worker)),
        ("merge", str(integration), str(worker)),
    ]
    assert sid in mc.merged


def test_merge_workspace_failure_halts_before_commit(tmp_path):
    from loopcore.ao_adapter import AOError

    mc, store = _mc(tmp_path)
    mc.step()
    _sid, task = _bind_done_worker(mc, store, "sess-gone")
    order = []

    def missing(session_id):
        order.append(("workspace", session_id))
        raise AOError("SESSION_WORKSPACE_NOT_FOUND")

    def kill(session_id):
        order.append(("kill", session_id))

    mc.adapter.get_session_workspace.side_effect = missing
    mc.executor.kill_worker = kill
    with patch("loopcore.mission.wt.commit_all") as commit:
        mc._merge_done()

    assert mc.state == "HUMAN"
    assert order == [
        ("workspace", task.worker_session_id),
        ("kill", task.worker_session_id),
    ]
    commit.assert_not_called()


def test_merge_commit_failure_preserves_git_detail(tmp_path):
    mc, store = _mc(tmp_path)
    mc.step()
    sid, task = _bind_done_worker(mc, store)
    worker = tmp_path / "actual-worker"
    worker.mkdir()
    mc.adapter.get_session_workspace.return_value = str(worker)

    with patch.object(mc.executor, "kill_worker") as kill, \
            patch("loopcore.mission.wt.commit_all",
                  side_effect=RuntimeError(
                      "git commit failed: last fake stderr")), \
            patch.object(mc, "_integration_wt") as integration, \
            patch("loopcore.mission.wt.merge_worktree") as merge:
        mc._merge_done()

    reason = mc._read_state().get("reason", "")
    assert mc.state == "HUMAN"
    assert "unable to commit Worker workspace for %s" % sid in reason
    assert "git commit failed: last fake stderr" in reason
    kill.assert_any_call(task.worker_session_id)
    integration.assert_not_called()
    merge.assert_not_called()
    assert mc.merged == []
    assert not (Path(store.path).parent / "integration").exists()


def test_full_single_lane_mission_to_done_without_task_llms(tmp_path):
    """The default clean lane uses only its Worker and Mission Verifier."""
    import subprocess

    project = tmp_path / "ao-data" / "worktrees" / "closed-loop-demo"
    project.mkdir(parents=True)

    def _git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)

    _git(project, "init", "-q")
    _git(project, "config", "user.name", "t")
    _git(project, "config", "user.email", "t@t")
    (project / "app.py").write_text("x=1\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "init")
    worker = project / "sess-S1"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "-q", "-b",
         "sess-S1", str(worker)],
        capture_output=True,
    )
    (worker / "app.py").write_text(
        "def clamp01(x):\n    return max(0, min(1, x))\n",
        encoding="utf-8",
    )

    verifier = _CountingVerifier()
    planner = MagicMock(wraps=FakePlannerProvider())
    mc, store = _mc(
        tmp_path,
        verifier=verifier,
        mission_data=SINGLE_MISSION,
        planner=planner,
    )
    mc.adapter.get_session_workspace.return_value = str(worker)
    task_audits = MagicMock(wraps=mc.auditor.audit)
    mc.auditor.audit = task_audits

    mc.step()
    planner.plan_decompose.assert_not_called()
    sid = mc.plan.subtasks[0].subtask_id
    with patch.object(mc.executor, "spawn_initial_worker",
                      return_value="sess-S1"):
        mc.step()

    gate_ok = MagicMock(ok=True, results=[
        {"command": "pytest", "stdout": "1 passed", "stderr": ""}],
        evidence=lambda: [{"type": "integration_gate", "summary": "pass",
                           "reference": "exit=0"}])
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        mc.loops[sid].step(injected_events=[ev(
            "2026-09-02T00:00:00Z", project=mc.mission.project_id,
            worker="sess-S1", etype="file_changed", progress=True,
            progress_strength="weak", message="modified app.py")])

    assert mc._subtask_state(sid) == ProjectState.DONE
    planner.plan.assert_not_called()
    assert store._conn.execute(
        "SELECT count(*) FROM verifications").fetchone()[0] == 0

    mc._merge_done()
    assert mc.merged == [sid]
    from loopcore.ao_adapter import AOError
    mc.adapter.get_session_workspace.reset_mock()
    mc.adapter.get_session_workspace.side_effect = AOError(
        "SESSION_WORKSPACE_NOT_FOUND")
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        result = mc.step()

    assert result["state"] == "MISSION_DONE"
    assert verifier.task_ids == [SINGLE_MISSION["mission_id"]]
    planner.plan_decompose.assert_not_called()
    planner.plan.assert_not_called()
    task_audits.assert_not_called()
    assert store._conn.execute(
        "SELECT task_id FROM verifications").fetchall() == [
            (SINGLE_MISSION["mission_id"],)]
    mc.adapter.get_session_workspace.assert_not_called()


def test_full_mission_to_done_with_merge(tmp_path):
    """Clean Tasks gate first; merged Mission still verifies exactly once."""
    import subprocess
    # real mini git repo with two AO Session workspaces
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
    verifier = _CountingVerifier()
    mc, store = _mc(tmp_path, verifier=verifier)
    mc.adapter.get_session_workspace.side_effect = (
        lambda session_id: str(wts[session_id]))
    mc.step()        # decompose
    task_audits = MagicMock(wraps=mc.auditor.audit)
    completion_plans = MagicMock(wraps=mc.planner.plan)
    mc.auditor.audit = task_audits
    mc.planner.plan = completion_plans
    spawned = {}
    def fake_spawn(task):
        sid = "sess-" + task.task_id[-2:]
        spawned[task.task_id] = sid
        return sid
    # Dispatch each dependency in order, then let each idle ClosedLoop discover
    # its real source change and take WORKER_RUNNING -> GATE_PENDING -> DONE.
    # The deterministic gate result is fake; no AO Worker/model is started.
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn):
        mc.step()    # dispatch S1 only (S2 dep)
    s1 = [s for s in mc.plan.subtasks if not s.dependencies][0].subtask_id
    s2 = [s for s in mc.plan.subtasks if s.dependencies][0].subtask_id
    gate_ok = MagicMock(ok=True, results=[
        {"command": "pytest", "stdout": "4 passed", "stderr": ""}],
        evidence=lambda: [{"type": "integration_gate", "summary": "pass",
                           "reference": "exit=0"}])
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        mc.loops[s1].step(injected_events=[ev(
            "2026-09-02T00:00:00Z", project=mc.mission.project_id,
            worker=mc.tasks[s1].worker_session_id, etype="file_changed",
            progress=True, progress_strength="weak",
            message="modified app.py")])
    assert mc._subtask_state(s1) == ProjectState.DONE
    assert verifier.task_ids == []
    with patch.object(mc.executor, "spawn_initial_worker",
                      side_effect=fake_spawn), \
            patch.object(IntegrationGate, "run", return_value=gate_ok):
        mc.step()    # S2 dispatch; S1 merge happens
    assert s1 in mc.merged, "S1 merged into integration worktree"
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        mc.loops[s2].step(injected_events=[ev(
            "2026-09-02T00:01:00Z", project=mc.mission.project_id,
            worker=mc.tasks[s2].worker_session_id, etype="file_changed",
            progress=True, progress_strength="weak",
            message="created math2.py")])
    assert mc._subtask_state(s2) == ProjectState.DONE
    assert verifier.task_ids == []
    task_audits.assert_not_called()
    completion_plans.assert_not_called()
    assert store._conn.execute(
        "SELECT count(*) FROM verifications").fetchone()[0] == 0
    # Merge S2 while its AO Session workspace remains available.
    mc._merge_done()
    assert s2 in mc.merged
    # Final verification must reuse the CL-AO integration worktree without
    # consulting either terminated Worker Session.
    from loopcore.ao_adapter import AOError
    mc.adapter.get_session_workspace.reset_mock()
    mc.adapter.get_session_workspace.side_effect = AOError(
        "SESSION_WORKSPACE_NOT_FOUND")
    with patch.object(IntegrationGate, "run", return_value=gate_ok):
        r = mc.step()
    assert r["state"] == "MISSION_DONE"
    assert verifier.task_ids == [mc.mission.mission_id]
    verification_rows = store._conn.execute(
        "SELECT task_id FROM verifications").fetchall()
    assert verification_rows == [(mc.mission.mission_id,)]
    mc.adapter.get_session_workspace.assert_not_called()
    # merged tree contains both subtask outputs
    integ = Path(store.path).parent / "integration"
    assert data_dir not in integ.parents
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
