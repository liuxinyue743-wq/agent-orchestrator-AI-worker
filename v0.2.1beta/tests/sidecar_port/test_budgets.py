"""V0.1 budget enforcement tests: max_runtime, max_same_alerts, replan-kill,
initial-worker-spawn. Uses fake providers + temp SQLite; no real AO/Claude."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from loopcore.action_executor import ActionExecutor, ActionResult
from loopcore.auditor import FakeAuditorProvider, EvidenceBundle
from loopcore.closed_loop import ClosedLoop
from loopcore.mission_contracts import (AuditDecision, AuditResult, AuditEvidence,
                           PlannerAction, PlannerActionType, ProjectState,
                           TaskSpec)
from loopcore.mission_gate import IntegrationGate
from loopcore.event_observer import Observer
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from tests.sidecar_port.test_contracts import _task_spec
from tests.sidecar_port.util import ev


def _cfg():
    return {
        "ao": {"base_url": "http://127.0.0.1:1", "request_timeout_seconds": 1,
               "sse_idle_timeout_seconds": 1, "poll_interval_seconds": 1},
        "thresholds": {
            "repeated_error": {"window_seconds": 600, "count": 3,
                               "cooldown_seconds": 0},
            "no_progress": {"window_seconds": 900, "min_activity_events": 8,
                            "max_progress_events": 0, "cooldown_seconds": 900,
                            "progress_mode": "strong"},
        },
    }


def _loop(tmp_path, *, dry=False, worker="w1", budgets=None):
    spec = _task_spec()
    if budgets:
        spec["budgets"] = budgets
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(spec)
    task.worker_session_id = worker
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": worker, "status": "idle"}
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    return ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                     planner=FakePlannerProvider(), executor=ex, observer=obs,
                     adapter=adapter, gate=gate, store=store,
                     dry_run=dry), task, store


def test_max_same_alerts_forces_human(tmp_path):
    """max_same_alerts=1 -> second same-fingerprint alert halts to HUMAN."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 5, "max_replans": 1, "max_same_alerts": 1,
        "max_runtime_seconds": 1800})
    errs = [ev("2026-08-27T00:0%d:00Z" % i, etype="error",
               message="conn refused to /x", fingerprint="fp1")
            for i in range(3)]
    loop._collect_events = MagicMock(return_value=errs)
    loop.step()                       # 1st alert -> audit (same_alert=1)
    assert loop.state != ProjectState.HUMAN
    # inject a different alert id so it isn't deduped; same fingerprint
    errs2 = [ev("2026-08-27T00:1%d:00Z" % i, etype="error",
                message="conn refused to /x", fingerprint="fp1")
             for i in range(3)]
    # change event_ids so observer treats them as fresh
    for e in errs2:
        e.event_id = e.event_id + "b"
    loop._collect_events = MagicMock(return_value=errs2)
    loop.step()                       # 2nd same-fp alert -> same_alert=2 > 1 -> HUMAN
    assert loop.state == ProjectState.HUMAN


def test_max_runtime_watchdog_halts(tmp_path):
    """max_runtime_seconds already exceeded -> HUMAN on next step."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 2, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1})
    loop._collect_events = MagicMock(return_value=[])
    loop.step()                       # stamps started_at
    import time
    time.sleep(2)
    loop._collect_events = MagicMock(return_value=[])
    loop.step()                       # watchdog trips
    assert loop.state == ProjectState.HUMAN


def test_replan_kills_old_worker(tmp_path):
    """REPLAN_SPAWN kills the old worker session before spawning new."""
    loop, task, store = _loop(tmp_path)
    audit = AuditResult("A1", task.task_id, AuditDecision.REPLAN,
                        [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"])
    pa = PlannerAction("ACT1", task.task_id, PlannerActionType.REPLAN_SPAWN,
                       reason="replan", target_session_id="w1",
                       replacement_task_spec={"objective": "new obj"})
    ex = loop.executor
    ex._run = MagicMock(return_value=MagicMock(returncode=0,
                          stdout="spawned session w2"))
    res = ex.execute(pa, task)
    # first call should have been `session kill w1`
    first_args = ex._run.call_args_list[0].args[0]
    assert first_args[:3] == ["session", "kill", "w1"]
    assert res.new_worker_session_id == "w2"


def test_kill_worker_calls_session_kill(tmp_path):
    loop, task, store = _loop(tmp_path)
    ex = loop.executor
    ex._run = MagicMock(return_value=MagicMock(returncode=0))
    assert ex.kill_worker("w1") is True
    assert ex._run.call_args.args[0] == ["session", "kill", "w1"]


def test_initial_spawn_in_loop(tmp_path):
    """No worker_session_id -> loop spawns initial worker (dry-run skips)."""
    spec = _task_spec()
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(spec)
    task.worker_session_id = None
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                     planner=FakePlannerProvider(), executor=ex, observer=obs,
                     adapter=adapter, gate=gate, store=store, dry_run=False)
    with patch.object(ex, "spawn_initial_worker", return_value="NEWW") as sp:
        loop.step()
        sp.assert_called_once()
    assert task.worker_session_id == "NEWW"
    assert loop.state == ProjectState.WORKER_RUNNING


def test_initial_spawn_skipped_in_dry_run(tmp_path):
    spec = _task_spec()
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(spec)
    task.worker_session_id = None
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock(); adapter.get_recent_events.return_value = []
    ex = ActionExecutor("ao", "d", "r", store)
    gate = IntegrationGate(store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                     planner=FakePlannerProvider(), executor=ex, observer=obs,
                     adapter=adapter, gate=gate, store=store, dry_run=True)
    with patch.object(ex, "spawn_initial_worker") as sp:
        loop.step()
        sp.assert_not_called()
    assert task.worker_session_id is None


def test_l0_nudge_single_error_no_audit(tmp_path):
    """L0: a single non-repeated error -> nudge the worker, no Auditor."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 2, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    # one error event (below repeated_error count=3 -> no L1 alert)
    errs = [ev("2026-08-27T00:00:00Z", etype="error",
               message="flake transient", fingerprint="fpL0")]
    loop._collect_events = MagicMock(return_value=errs)
    loop.auditor = MagicMock()
    loop.executor.nudge_worker = MagicMock(return_value=True)
    # past the L0 hatch grace + worker idle (nudges only fire then — real
    # runs showed mid-turn nudges get rejected by AO and kill the turn)
    loop._worker_status = MagicMock(return_value={
        "id": "w", "status": "idle", "activity": {"state": "idle"}})
    store.counter_set("hatched_at:%s:%s" % (task.task_id, task.worker_session_id), 1)
    loop.step()
    loop.auditor.audit.assert_not_called()          # L1 not triggered
    loop.executor.nudge_worker.assert_called_once()  # L0 sent
    # second step same fingerprint -> no re-nudge (deduped). With the nudge
    # deduped and the worker idle, the 簇二 elif-shadowing fix lets the loop
    # proceed to the completion audit (a historical error event must not
    # block it forever) — give the mock auditor a serializable PASS so the
    # audit record is storable.
    loop.executor.nudge_worker.reset_mock()
    loop.auditor.audit.return_value = AuditResult(
        audit_id="a-l0-2", task_id=task.task_id, decision=AuditDecision.PASS,
        evidence=[], diagnosis="ok", confidence=1.0)
    loop.step()
    loop.executor.nudge_worker.assert_not_called()


def test_l1_alert_shadows_l0(tmp_path):
    """L1 REPEATED_ERROR -> Auditor, NOT L0 nudge."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 5, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    errs = [ev("2026-08-27T00:0%d:00Z" % i, etype="error",
               message="conn refused", fingerprint="fpX")
            for i in range(3)]   # 3 same -> REPEATED_ERROR alert (count=3)
    loop._collect_events = MagicMock(return_value=errs)
    loop.executor.nudge_worker = MagicMock()
    loop.step()
    # Auditor was invoked (FakeAuditor), and nudge NOT called (L1 won)
    loop.executor.nudge_worker.assert_not_called()


def test_active_repeated_error_waits_for_completion_evidence(tmp_path):
    """A real L1 alert is durable, but never audits an active AO turn."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 5, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop.adapter.get_worker_conversation.return_value = {"activities": []}
    loop.adapter.get_worker_status.return_value = {
        "id": task.worker_session_id, "status": "running",
        "activity": {"state": "active"}}
    errors = [
        ev("2026-08-27T00:0%d:00Z" % i,
           project=task.project_id, worker=task.worker_session_id,
           etype="error", message="provider transport failed",
           fingerprint="provider-fp")
        for i in range(3)
    ]
    loop._collect_events = MagicMock(return_value=errors)
    loop.auditor = MagicMock()
    loop.planner = MagicMock()
    loop.executor.nudge_worker = MagicMock()
    loop.executor.execute = MagicMock()
    loop._run_gate_capture = MagicMock()
    loop._to_planner = MagicMock()

    first = loop.step()

    alerts = store._conn.execute(
        "SELECT payload_json FROM alerts").fetchall()
    assert len(alerts) == 1
    assert json.loads(alerts[0][0])["alert_type"] == "REPEATED_ERROR"
    assert first["acted"] is False
    assert loop.state == ProjectState.WORKER_RUNNING
    loop.auditor.audit.assert_not_called()
    loop.planner.plan.assert_not_called()
    loop.executor.nudge_worker.assert_not_called()
    loop.executor.execute.assert_not_called()
    loop._run_gate_capture.assert_not_called()
    loop._to_planner.assert_not_called()

    # The same Worker later completes its file change and becomes idle.  Move
    # the event timestamp beyond the repeated-error window so this tick is the
    # existing quiet-completion path, not a second synthetic alert.
    completed = [ev(
        "2026-08-27T00:20:00Z", project=task.project_id,
        worker=task.worker_session_id, etype="file_changed",
        activity=True, progress=True, message="edited app.py")]
    loop._collect_events.return_value = completed
    loop.adapter.get_worker_status.return_value = {
        "id": task.worker_session_id, "status": "idle",
        "activity": {"state": "idle"}}
    completed_run = MagicMock(ok=True)
    loop._run_gate_capture.return_value = (
        completed_run, "completed workspace gate: PASS")
    loop._git_diff = MagicMock(return_value="completed app.py diff")
    captured = []

    def audit_completed(bundle, audit_id):
        captured.append(bundle)
        return AuditResult(
            audit_id=audit_id, task_id=task.task_id,
            decision=AuditDecision.PASS, evidence=[], diagnosis="complete",
            confidence=1.0)

    loop.auditor.audit.side_effect = audit_completed
    second = loop.step()

    assert second["acted"] is True
    assert len(captured) == 1
    assert captured[0].audit_type == "COMPLETION"
    assert captured[0].test_output == "completed workspace gate: PASS"
    assert captured[0].git_diff == "completed app.py diff"
    assert captured[0].failed_criteria == []
    loop._to_planner.assert_called_once()
    loop.executor.nudge_worker.assert_not_called()


def test_active_repeated_error_preserves_mixed_no_progress_alert(tmp_path):
    """Only REPEATED_ERROR is deferred; NO_PROGRESS remains actionable."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 5, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop.adapter.get_worker_conversation.return_value = {"activities": []}
    loop.adapter.get_worker_status.return_value = {
        "id": task.worker_session_id, "status": "running",
        "activity": {"state": "active"}}
    events = [
        ev("2026-08-27T00:0%d:00Z" % i,
           project=task.project_id, worker=task.worker_session_id,
           etype="command_executed", message="command #%d" % i)
        for i in range(5)
    ]
    # The eighth event is both the third same-fingerprint error and the
    # eighth activity without strong progress, so one Observer evaluation
    # returns REPEATED_ERROR + NO_PROGRESS together.
    events += [
        ev("2026-08-27T00:0%d:00Z" % i,
           project=task.project_id, worker=task.worker_session_id,
           etype="error", message="provider transport failed",
           fingerprint="provider-fp")
        for i in range(5, 8)
    ]
    loop._collect_events = MagicMock(return_value=events)
    loop.executor.nudge_worker = MagicMock()
    loop._run_gate_capture = MagicMock(return_value=(None, ""))
    loop._git_diff = MagicMock(return_value="active workspace diff")
    loop.auditor = MagicMock()
    loop.auditor.audit.return_value = AuditResult(
        audit_id="AUDIT-NO-PROGRESS", task_id=task.task_id,
        decision=AuditDecision.LOCAL_FIX, evidence=[],
        diagnosis="no progress", confidence=0.9,
        failed_criteria=["AC-01"])
    loop._to_planner = MagicMock()
    handle_alerts = loop._handle_alerts
    loop._handle_alerts = MagicMock(wraps=handle_alerts)

    result = loop.step()

    persisted = [json.loads(row[0])["alert_type"] for row in
                 store._conn.execute(
                     "SELECT payload_json FROM alerts").fetchall()]
    assert sorted(persisted) == ["NO_PROGRESS", "REPEATED_ERROR"]
    loop._handle_alerts.assert_called_once()
    actionable = loop._handle_alerts.call_args.args[0]
    assert [alert.alert_type for alert in actionable] == ["NO_PROGRESS"]
    audited_bundle = loop.auditor.audit.call_args.args[0]
    assert audited_bundle.alert["alert_type"] == "NO_PROGRESS"
    assert [alert["alert_type"] for alert in audited_bundle.alerts] == [
        "NO_PROGRESS"]
    assert result["acted"] is True
    assert loop.state == ProjectState.AUDIT_PENDING
    loop._to_planner.assert_called_once()
    loop.executor.nudge_worker.assert_not_called()


def test_l0_does_not_consume_local_fix_budget(tmp_path):
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 2, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    errs = [ev("2026-08-27T00:00:00Z", etype="error",
               message="flake", fingerprint="fpL0")]
    loop._collect_events = MagicMock(return_value=errs)
    loop.executor.nudge_worker = MagicMock(return_value=True)
    loop.step()
    # local_fixes counter must still be 0 (L0 doesn't count)
    assert store.counter_get("local_fixes:" + task.task_id) == 0


def test_alert_aggregation_single_audit_per_cycle(tmp_path):
    """A burst of N distinct-fingerprint alerts -> ONE aggregated audit."""
    loop, task, store = _loop(tmp_path, budgets={
        "max_local_fixes": 5, "max_replans": 1, "max_same_alerts": 5,
        "max_runtime_seconds": 1800})
    # two different fingerprints, each crossing repeated_error count=3 -> two alerts
    errs = []
    for fp in ("fpA", "fpB"):
        errs += [ev("2026-08-27T00:0%d:00Z" % i, etype="error",
                    message="conn refused", fingerprint=fp)
                 for i in range(3)]
    loop._collect_events = MagicMock(return_value=errs)
    calls = []
    def fake_audit(bundle, audit_id):
        calls.append(audit_id)
        return AuditResult(audit_id, task.task_id, AuditDecision.LOCAL_FIX,
                           [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"])
    loop.auditor.audit = fake_audit
    loop.step()
    assert len(calls) == 1          # aggregated, not one audit per alert
    assert len(calls[0]) > 0


def test_replan_spawn_writes_back_session_id(tmp_path):
    """REPLAN_SPAWN's new worker session id is written back to the TaskSpec."""
    loop, task, store = _loop(tmp_path)
    loop.executor.execute = MagicMock(return_value=ActionResult(
        "ACT1", PlannerActionType.REPLAN_SPAWN, True, "ok",
        new_state=ProjectState.WORKER_RUNNING, new_worker_session_id="w2"))
    pa = PlannerAction("ACT1", task.task_id, PlannerActionType.REPLAN_SPAWN,
                       reason="replan")
    loop._execute(pa, AuditResult("A1", task.task_id, AuditDecision.REPLAN,
                                  [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"]))
    assert task.worker_session_id == "w2"


def test_instruct_reaches_planner(tmp_path):
    """The top-level user directive is threaded into the Planner call."""
    loop, task, store = _loop(tmp_path)
    loop.instruct = "优先测试全绿，禁止改 tests"
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop._transition(ProjectState.AUDIT_PENDING, "test", "setup", {})
    loop.planner = MagicMock()
    loop.planner.plan = MagicMock(return_value=PlannerAction(
        "ACT1", task.task_id, PlannerActionType.CONTINUE, reason="r"))
    loop._to_planner(AuditResult("A1", task.task_id, AuditDecision.LOCAL_FIX,
                                 [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"]))
    assert loop.planner.plan.call_args.kwargs["instruct"] == "优先测试全绿，禁止改 tests"


def test_idle_worker_triggers_completion_audit(tmp_path):
    """Quiet completion: idle worker, no alerts, no errors -> one audit."""
    loop, task, store = _loop(tmp_path)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    # quiet events (no error events -> no L0, no repeated errors -> no L1)
    quiet = [ev("2026-08-27T00:0%d:00Z" % i, etype="command_executed",
                message="pytest")
             for i in range(3)]
    loop._collect_events = MagicMock(return_value=quiet)
    loop._completion_audit = MagicMock()
    loop.step()
    loop._completion_audit.assert_called_once()


def test_idle_completion_paced_by_cooldown(tmp_path):
    """Second idle-completion step within cooldown does NOT re-audit."""
    loop, task, store = _loop(tmp_path)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    quiet = [ev("2026-08-27T00:0%d:00Z" % i, etype="command_executed",
                message="pytest")
             for i in range(3)]
    loop._collect_events = MagicMock(return_value=quiet)
    loop._completion_audit = MagicMock()
    loop.step()
    loop.step()
    assert loop._completion_audit.call_count == 1


def test_busy_worker_no_completion_audit(tmp_path):
    """A worker still running (activity != idle) is left alone."""
    loop, task, store = _loop(tmp_path)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop.adapter.get_worker_status.return_value = {
        "id": "w1", "status": "running",
        "activity": {"state": "running"}}
    quiet = [ev("2026-08-27T00:00:00Z", etype="command_executed",
                message="pytest")]
    loop._collect_events = MagicMock(return_value=quiet)
    loop._completion_audit = MagicMock()
    loop.step()
    loop._completion_audit.assert_not_called()



def test_auto_approve_inside_allowed_paths_only(tmp_path, monkeypatch):
    """Pending approvals: edits inside allowed_paths -> allow_once resolved;
    outside/tests edits stay pending for the human.

    簇三: the file branch now resolves REAL paths against the worker's
    worktree (approvals.decide_approval), so the test builds a real
    worktree on disk instead of synthetic E:\\w\\... strings."""
    import subprocess
    loop, task, store = _loop(tmp_path)
    wt = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
    loop.adapter.get_session_workspace.return_value = str(wt)
    def _act(aid, fpath):
        return {"id": aid, "activityKind": "approval", "status": "pending",
                "providerItemId": aid, "summary": "Edit x",
                "detail": {"input": {"file_path": fpath},
                           "subjectKind": "file_change"}}
    conv = {"activities": [
        _act("ap1", str(wt / "app.py")),
        _act("ap2", str(wt / "tests" / "test_divide.py")),
        _act("ap3", str(wt / "other.py")),
        {"id": "ap4", "activityKind": "approval", "status": "completed"},
    ]}
    loop.adapter.get_worker_conversation = MagicMock(return_value=conv)
    loop.adapter.resolve_approval = MagicMock(return_value=True)
    store.record_transition(task_id=task.task_id, from_state="TASK_READY",
                            to_state="WORKER_RUNNING", actor="t", reason="t",
                            evidence={})
    fired = loop._maybe_auto_approve()
    assert fired
    approved = [c.args[1] for c in
                loop.adapter.resolve_approval.call_args_list]
    assert approved == ["ap1"], approved          # only the in-scope edit
    # idempotent second pass: ap1 resolved+deduped, others recorded as seen
    loop.adapter.resolve_approval.reset_mock()
    assert not loop._maybe_auto_approve()
    loop.adapter.resolve_approval.assert_not_called()
