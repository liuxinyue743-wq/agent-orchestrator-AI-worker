"""Gate-first completion regressions for a clean first Worker attempt."""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission import MissionController
from loopcore.mission_contracts import (
    AuditDecision,
    AuditEvidence,
    AuditResult,
    PlannerAction,
    PlannerActionType,
    ProjectState,
)
from loopcore.mission_gate import IntegrationGate
from tests.sidecar_port.test_verifier import _ScriptedVerifier, _loop
from tests.sidecar_port.util import ev


def _running_loop(tmp_path, *, gate_ok=True):
    verifier = _ScriptedVerifier("PASS")
    loop, task, store = _loop(
        tmp_path, verifier, fake_gate_run_ok=gate_ok)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop.adapter.get_worker_conversation.return_value = {"activities": []}
    return loop, task, store, verifier


def _completed_event(task, minute=0):
    return ev(
        "2026-09-02T00:%02d:00Z" % minute,
        project=task.project_id,
        worker=task.worker_session_id,
        etype="file_changed",
        progress=True,
        progress_strength="weak",
        message="modified app.py",
    )


def _to_retrying(loop):
    loop._transition(ProjectState.AUDIT_PENDING, "test", "setup", {})
    loop._transition(ProjectState.PLANNER_PENDING, "test", "setup", {})
    loop._transition(ProjectState.LOCAL_FIX_PENDING, "test", "setup", {})
    loop._transition(ProjectState.WORKER_RETRYING, "test", "setup", {})


def _routing_controller(project_id, snapshots=()):
    controller = MissionController.__new__(MissionController)
    controller.mission = SimpleNamespace(project_id=project_id)
    controller.adapter = MagicMock()
    controller.adapter.get_recent_events.side_effect = list(snapshots)
    return controller


def _raw_session(worker_id, project_id, state, timestamp):
    return {
        "kind": "session",
        "session_id": worker_id,
        "session": {
            "id": worker_id,
            "projectId": project_id,
            "activity": {"state": state, "lastActivityAt": timestamp},
        },
    }


def _raw_turn(worker_id, *, with_diff=False):
    return {
        "kind": "turn",
        "session_id": worker_id,
        "turn": {
            "id": "turn-1",
            "state": "completed",
            "requestedAt": "2026-09-02T00:00:00Z",
            "completedAt": "2026-09-02T00:01:00Z",
            "diff": ({"files": [{"path": "app.py"}]}
                     if with_diff else {}),
        },
    }


def _raw_error(worker_id, sequence, *, activity_id=None):
    return {
        "kind": "activity",
        "session_id": worker_id,
        "activity": {
            "id": activity_id or "error-%d" % sequence,
            "activityKind": "error",
            "status": "failed",
            "summary": "provider connection failed",
            "turnId": "turn-1",
            "sequence": sequence,
        },
    }


def test_idle_changed_source_gate_pass_skips_completion_roles(tmp_path):
    loop, task, store, verifier = _running_loop(tmp_path)
    loop.auditor.audit = MagicMock()
    loop.planner.plan = MagicMock()
    loop._completion_audit = MagicMock()

    result = loop.step(injected_events=[_completed_event(task)])

    assert result == {"state": ProjectState.DONE, "acted": True}
    loop.gate.run.assert_called_once()
    loop._completion_audit.assert_not_called()
    loop.auditor.audit.assert_not_called()
    loop.planner.plan.assert_not_called()
    assert verifier.inputs == []
    transitions = store._conn.execute(
        "SELECT to_state FROM state_transitions "
        "WHERE task_id=? ORDER BY id", (task.task_id,)).fetchall()
    assert transitions == [
        (ProjectState.WORKER_RUNNING,),
        (ProjectState.GATE_PENDING,),
        (ProjectState.DONE,),
    ]


def test_idle_changed_source_gate_fail_uses_auditor_and_planner(tmp_path):
    loop, task, store, verifier = _running_loop(tmp_path, gate_ok=False)
    audit = AuditResult(
        "A-GATE-FIRST-FAIL", task.task_id, AuditDecision.HUMAN,
        [AuditEvidence("test_failure", "gate failed")],
        "gate failed", 1.0, ["AC-01"])
    loop.auditor.audit = MagicMock(return_value=audit)
    loop.planner.plan = MagicMock(return_value=PlannerAction(
        "ACT-GATE-FIRST-FAIL", task.task_id, PlannerActionType.HUMAN,
        reason="gate failure requires human"))
    loop.executor.execute = MagicMock(return_value=MagicMock(
        ok=True, new_state=ProjectState.HUMAN,
        new_worker_session_id=None, detail="halted"))

    result = loop.step(injected_events=[_completed_event(task)])

    assert result["state"] == ProjectState.HUMAN
    assert loop.state != ProjectState.DONE
    loop.gate.run.assert_called_once()
    loop.auditor.audit.assert_called_once()
    loop.planner.plan.assert_called_once()
    assert verifier.inputs == []
    transitions = store._conn.execute(
        "SELECT to_state FROM state_transitions "
        "WHERE task_id=? ORDER BY id", (task.task_id,)).fetchall()
    assert transitions[:4] == [
        (ProjectState.WORKER_RUNNING,),
        (ProjectState.GATE_PENDING,),
        (ProjectState.AUDIT_PENDING,),
        (ProjectState.PLANNER_PENDING,),
    ]


def test_task_gate_exit_zero_source_mutation_uses_failure_path(tmp_path):
    loop, task, store, verifier = _running_loop(tmp_path)
    task.gate_commands = [
        '"%s" -c "from pathlib import Path; '
        "p=Path('app.py'); p.write_text(p.read_text()+'# gate mutation')\""
        % sys.executable]
    loop.gate = IntegrationGate(store)
    audit = AuditResult(
        "A-GATE-INTEGRITY", task.task_id, AuditDecision.HUMAN,
        [AuditEvidence("gate_repository_integrity", "gate changed source")],
        "gate changed source", 1.0, ["AC-01"])
    loop.auditor.audit = MagicMock(return_value=audit)
    loop.planner.plan = MagicMock(return_value=PlannerAction(
        "ACT-GATE-INTEGRITY", task.task_id, PlannerActionType.HUMAN,
        reason="gate integrity failure requires human"))
    loop.executor.execute = MagicMock(return_value=MagicMock(
        ok=True, new_state=ProjectState.HUMAN,
        new_worker_session_id=None, detail="halted"))
    loop._transition(ProjectState.GATE_PENDING, "test", "setup", {})

    loop._run_gate()

    assert loop.state == ProjectState.HUMAN
    assert loop.state != ProjectState.DONE
    assert verifier.inputs == []
    bundle = loop.auditor.audit.call_args.args[0]
    assert "[gate repository integrity] Gate changed repository state" \
        in bundle.test_output
    assert store._conn.execute(
        "SELECT exit_code FROM gate_runs WHERE task_id=? ORDER BY id DESC",
        (task.task_id,)).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT count(*) FROM state_transitions WHERE task_id=? "
        "AND to_state=? AND actor=?",
        (task.task_id, ProjectState.AUDIT_PENDING,
         "integration_gate")).fetchone()[0] == 1


def test_completion_gate_capture_includes_integrity_failure(tmp_path):
    loop, task, store, _verifier = _running_loop(tmp_path)
    task.gate_commands = [
        '"%s" -c "from pathlib import Path; '
        "p=Path('app.py'); p.write_text(p.read_text()+'# capture mutation')\""
        % sys.executable]
    loop.gate = IntegrationGate(store)

    run, test_output = loop._run_gate_capture()

    assert run.command_ok is True
    assert run.integrity_ok is False
    assert "[gate repository integrity] Gate changed repository state" \
        in test_output


def test_empty_gate_commands_keep_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    task.gate_commands = []
    loop._completion_audit = MagicMock()

    loop.step(injected_events=[_completed_event(task)])

    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_no_changed_paths_keeps_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop._completion_audit = MagicMock()

    with patch("loopcore.closed_loop.wt.changed_paths", return_value=[]):
        loop.step(injected_events=[_completed_event(task)])

    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_unknown_changed_paths_keeps_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop._completion_audit = MagicMock()

    with patch("loopcore.closed_loop.wt.changed_paths", return_value=None):
        loop.step(injected_events=[_completed_event(task)])

    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_unresolved_workspace_keeps_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop.adapter.get_session_workspace.return_value = None
    loop._completion_audit = MagicMock()

    loop.step(injected_events=[_completed_event(task)])

    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_pending_approval_blocks_gate_first_completion(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    worktree = loop.adapter.get_session_workspace.return_value
    loop.adapter.get_worker_conversation.return_value = {
        "activities": [{
            "id": "approval-outside",
            "activityKind": "approval",
            "status": "pending",
            "detail": {"input": {"file_path": worktree + "/outside.py"}},
        }]}
    loop._completion_audit = MagicMock()

    loop.step(injected_events=[_completed_event(task)])

    assert loop.state == ProjectState.WORKER_RUNNING
    loop.gate.run.assert_not_called()
    loop._completion_audit.assert_not_called()


def test_fresh_error_awaiting_l0_keeps_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop._completion_audit = MagicMock()
    loop.executor.nudge_worker = MagicMock()
    error = ev(
        "2026-09-02T00:00:00Z",
        project=task.project_id,
        worker=task.worker_session_id,
        etype="error",
        message="fresh failure",
        fingerprint="fresh-failure",
    )

    loop.step(injected_events=[error])

    # First sighting stamps the hatch-grace clock; the L0 nudge is still
    # pending, so this tick must not be reclassified as a clean Gate-first.
    loop.executor.nudge_worker.assert_not_called()
    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_actionable_repeated_error_blocks_gate_first_completion(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop._handle_alerts = MagicMock()
    errors = [
        ev(
            "2026-09-02T00:0%d:00Z" % i,
            project=task.project_id,
            worker=task.worker_session_id,
            etype="error",
            message="same failure",
            fingerprint="same-failure",
        )
        for i in range(3)
    ]

    loop.step(injected_events=errors)

    loop._handle_alerts.assert_called_once()
    loop.gate.run.assert_not_called()


def test_actionable_no_progress_blocks_gate_first_completion(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    loop._handle_alerts = MagicMock()
    activity = [
        ev(
            "2026-09-02T00:%02d:00Z" % i,
            project=task.project_id,
            worker=task.worker_session_id,
            etype="command_executed",
            message="command %d" % i,
        )
        for i in range(8)
    ]

    loop.step(injected_events=activity)

    loop._handle_alerts.assert_called_once()
    loop.gate.run.assert_not_called()


def test_worker_retrying_idle_keeps_completion_audit(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    _to_retrying(loop)
    loop._completion_audit = MagicMock()

    loop.step(injected_events=[_completed_event(task)])

    loop._completion_audit.assert_called_once()
    loop.gate.run.assert_not_called()


def test_mission_replayed_errors_do_not_block_gate_first(tmp_path):
    """A full-history Mission poll must not make old AO errors fresh again."""
    loop, task, store, _verifier = _running_loop(tmp_path)
    # _running_loop reuses the budget-test config whose alert cooldown is 0.
    # This scenario's real precondition is no new actionable alert on tick 2,
    # so retain the production repeated-error cooldown instead.
    loop.observer.re_err["cooldown_seconds"] = 600
    worker_id = task.worker_session_id
    errors = [_raw_error(worker_id, sequence) for sequence in (2, 3, 4)]
    active = [
        _raw_session(worker_id, task.project_id, "active",
                     "2026-09-02T00:01:00Z"),
        _raw_turn(worker_id),
        *errors,
    ]
    idle_replay = [
        _raw_session(worker_id, task.project_id, "idle",
                     "2026-09-02T00:02:00Z"),
        _raw_turn(worker_id),
        *errors,
    ]
    controller = _routing_controller(
        task.project_id, snapshots=[active, idle_replay])
    loop.auditor.audit = MagicMock(wraps=loop.auditor.audit)
    loop.planner.plan = MagicMock(wraps=loop.planner.plan)

    loop.adapter.get_worker_status.return_value = {
        "id": worker_id, "activity": {"state": "active"}}
    controller._collect_all_events()
    first_events = controller._route_events(loop, worker_id)
    loop.step(injected_events=first_events)

    alert_payloads = [row[0] for row in store._conn.execute(
        "SELECT payload_json FROM alerts").fetchall()]
    assert any('"alert_type": "REPEATED_ERROR"' in payload
               for payload in alert_payloads)
    assert loop.state == ProjectState.WORKER_RUNNING
    assert store.counter_get(
        "hatched_at:%s:%s" % (task.task_id, worker_id)) == 0

    loop.adapter.get_worker_status.return_value = {
        "id": worker_id, "activity": {"state": "idle"}}
    original_idle_completion = loop._maybe_idle_completion
    original_gate_first = loop._try_gate_first_completion
    loop._maybe_idle_completion = MagicMock(wraps=original_idle_completion)
    loop._try_gate_first_completion = MagicMock(wraps=original_gate_first)
    controller._collect_all_events()
    second_events = controller._route_events(loop, worker_id)
    result = loop.step(injected_events=second_events)

    assert all(event.event_type != "error" for event in second_events)
    assert loop._event_since[worker_id] == 4
    assert controller.adapter.get_recent_events.call_args_list == [
        ((task.project_id,), {"since": 0}),
        ((task.project_id,), {"since": 0}),
    ]
    assert loop._maybe_idle_completion.call_args.kwargs[
        "allow_gate_first"] is True
    loop._try_gate_first_completion.assert_called_once()
    assert result == {"state": ProjectState.DONE, "acted": True}
    assert store.counter_get(
        "hatched_at:%s:%s" % (task.task_id, worker_id)) == 0
    loop.auditor.audit.assert_not_called()
    loop.planner.plan.assert_not_called()
    transitions = store._conn.execute(
        "SELECT to_state FROM state_transitions "
        "WHERE task_id=? ORDER BY id", (task.task_id,)).fetchall()
    assert transitions == [
        (ProjectState.WORKER_RUNNING,),
        (ProjectState.GATE_PENDING,),
        (ProjectState.DONE,),
    ]


def test_shared_route_replays_sessions_and_turns_but_not_activities(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    worker_id = task.worker_session_id
    controller = _routing_controller(task.project_id)
    controller._last_raw_items = [
        _raw_session(worker_id, task.project_id, "active",
                     "2026-09-02T00:00:00Z"),
        _raw_turn(worker_id, with_diff=True),
        _raw_error(worker_id, 2),
    ]

    first = controller._route_events(loop, worker_id)
    second = controller._route_events(loop, worker_id)

    assert {event.event_type for event in first} == {
        "worker_started", "file_changed", "error"}
    assert [event.event_type for event in second] == ["file_changed"]
    assert loop._event_since[worker_id] == 2


def test_shared_route_activity_cursors_are_isolated_per_worker(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    controller = _routing_controller(task.project_id)
    controller._last_raw_items = [
        _raw_error("worker-a", 20),
        _raw_error("worker-b", 1),
        _raw_error("worker-b", 2),
        _raw_error("worker-b", 3),
    ]

    events_a = controller._route_events(loop, "worker-a")
    events_b = controller._route_events(loop, "worker-b")

    assert [event.evidence["sequence"] for event in events_a] == [20]
    assert [event.evidence["sequence"] for event in events_b] == [1, 2, 3]
    assert loop._event_since == {"worker-a": 20, "worker-b": 3}


def test_shared_route_replan_worker_starts_with_own_cursor(tmp_path):
    loop, task, _store, _verifier = _running_loop(tmp_path)
    controller = _routing_controller(task.project_id)
    controller._last_raw_items = [
        _raw_error("worker-old", 20),
        _raw_error("worker-replan", 1),
    ]

    loop.task.worker_session_id = "worker-old"
    controller._route_events(loop, loop.task.worker_session_id)
    loop.task.worker_session_id = "worker-replan"
    replan_events = controller._route_events(
        loop, loop.task.worker_session_id)

    assert [event.evidence["sequence"] for event in replan_events] == [1]
    assert loop._event_since == {"worker-old": 20, "worker-replan": 1}


def test_persistent_event_seen_survives_closed_loop_restart(tmp_path):
    loop, task, store, verifier = _running_loop(tmp_path)
    historical = ev(
        "2026-09-02T00:00:00Z",
        project=task.project_id,
        worker=task.worker_session_id,
        etype="error",
        message="historical failure",
        fingerprint="historical-failure",
    )
    loop.observer.feed(historical)
    assert store.event_seen(historical.event_id)

    restarted = ClosedLoop(
        task=task, cfg=loop.cfg, auditor=loop.auditor,
        planner=loop.planner, executor=loop.executor,
        observer=Observer(loop.cfg, state_store=store),
        adapter=loop.adapter, gate=loop.gate, store=store,
        verifier=verifier)
    assert restarted._event_since == {}
    restarted._completion_audit = MagicMock()
    original_gate_first = restarted._try_gate_first_completion
    restarted._try_gate_first_completion = MagicMock(
        wraps=original_gate_first)
    restarted.adapter.get_worker_status.return_value = {
        "id": task.worker_session_id, "activity": {"state": "idle"}}

    result = restarted.step(injected_events=[historical])

    restarted._try_gate_first_completion.assert_called_once()
    restarted._completion_audit.assert_not_called()
    assert result == {"state": ProjectState.DONE, "acted": True}
    assert store.counter_get(
        "hatched_at:%s:%s" %
        (task.task_id, task.worker_session_id)) == 0
