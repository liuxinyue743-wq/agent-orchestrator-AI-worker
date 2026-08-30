"""Phase 2 tests: auditor/planner/action executor (no real AO/Claude)."""
import json
from unittest.mock import MagicMock, patch

from src.auditor import (EvidenceBundle, FakeAuditorProvider,
                         ClaudeCliAuditorProvider)
from src.contracts import (AuditDecision, AuditResult, AuditEvidence,
                           PlannerAction, PlannerActionType, ProjectState,
                           TaskSpec)
from src.planner_adapter import FakePlannerProvider
from src.action_executor import ActionExecutor
from src.state_store import StateStore
from tests.test_contracts import _task_spec


def _bundle(failed=None):
    ts = _task_spec()
    return EvidenceBundle(task_spec=ts, alert={"alert_type": "REPEATED_ERROR"},
                          failed_criteria=failed or [])


# --- auditor -------------------------------------------------------------
def test_fake_auditor_local_fix():
    p = FakeAuditorProvider()
    ar = p.audit(_bundle(["AC-01"]), "A1")
    assert ar.decision == AuditDecision.LOCAL_FIX
    assert ar.validate()[0]
    assert ar.evidence  # non-empty


def test_fake_auditor_pass():
    p = FakeAuditorProvider()
    ar = p.audit(_bundle([]), "A2")
    assert ar.decision == AuditDecision.PASS


def test_invalid_auditor_output_to_human():
    """ClaudeCliAuditor: two invalid outputs -> HUMAN."""
    prov = ClaudeCliAuditorProvider(claude_bin="fake")
    # force _call to return invalid dict twice
    prov._call = MagicMock(return_value={"decision": "BOGUS", "evidence": []})
    ar = prov.audit(_bundle(["AC-01"]), "A3")
    assert ar.decision == AuditDecision.HUMAN
    assert ar.evidence
    assert "format" in ar.evidence[0].summary.lower() or \
           "invalid" in ar.evidence[0].summary.lower() or \
           "schema" in ar.evidence[0].summary.lower()


def test_auditor_one_retry_then_ok():
    prov = ClaudeCliAuditorProvider(claude_bin="fake")
    good = {"audit_id": "A4", "task_id": "TASK-DEMO-001",
            "decision": "LOCAL_FIX",
            "evidence": [{"type": "t", "summary": "s"}],
            "diagnosis": "d", "confidence": 0.9}
    calls = iter([{"bad": 1}, good])
    prov._call = MagicMock(side_effect=lambda b, a, use_schema=True: next(calls))
    ar = prov.audit(_bundle(["AC-01"]), "A4")
    assert ar.decision == "LOCAL_FIX"


# --- planner ------------------------------------------------------------
def test_fake_planner_local_fix():
    p = FakePlannerProvider()
    audit = AuditResult("A1", "T1", AuditDecision.LOCAL_FIX,
                        [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"])
    pa = p.plan(audit, {"task_id": "T1"}, "ACT1",
               target_session_id="w1")
    assert pa.action == PlannerActionType.SEND_LOCAL_FIX
    assert pa.target_session_id == "w1"
    ok, _ = pa.validate()
    assert ok


def test_fake_planner_pass_to_candidate_done():
    p = FakePlannerProvider()
    audit = AuditResult("A2", "T1", AuditDecision.PASS,
                        [AuditEvidence("t", "s")], "d", 0.9)
    pa = p.plan(audit, {"task_id": "T1"}, "ACT2")
    assert pa.action == PlannerActionType.CANDIDATE_DONE


def test_planner_replan_exhausted_to_human():
    p = FakePlannerProvider()
    audit = AuditResult("A3", "T1", AuditDecision.REPLAN,
                        [AuditEvidence("t", "s")], "d", 0.9)
    pa = p.plan(audit, {"task_id": "T1"}, "ACT3", remaining_replans=0)
    assert pa.action == PlannerActionType.HUMAN


def test_invalid_planner_output_to_human():
    """AOOrchestratorPlannerProvider returning invalid JSON twice -> HUMAN."""
    from src.planner_adapter import AOOrchestratorPlannerProvider
    prov = AOOrchestratorPlannerProvider(
        ao_bin="fake", project_id="p", data_dir="d", run_file="r")
    # force _call to return an invalid object twice
    prov._call = MagicMock(side_effect=lambda *a, **k: {"action": "Bogus"})
    audit = AuditResult("A4", "T1", AuditDecision.LOCAL_FIX,
                        [AuditEvidence("t", "s")], "d", 0.9, ["AC-01"])
    pa = prov.plan(audit, {"task_id": "T1"}, "ACT4", target_session_id="w1")
    assert pa.action == PlannerActionType.HUMAN


# --- action executor ----------------------------------------------------
def _exec(tmp_path):
    store = StateStore(tmp_path / "cl.db")
    return ActionExecutor(ao_bin="ao", data_dir="d", run_file="r",
                          store=store), store


def test_action_idempotency(tmp_path):
    ex, store = _exec(tmp_path)
    pa = PlannerAction("A1", "T", PlannerActionType.CONTINUE, "r")
    r1 = ex.execute(pa, TaskSpec.from_dict(_task_spec()))
    r2 = ex.execute(pa, TaskSpec.from_dict(_task_spec()))
    assert r1.ok
    assert r2.detail == "already executed (idempotent)"


def test_local_fix_dispatch(tmp_path):
    ex, store = _exec(tmp_path)
    pa = PlannerAction("A2", "T", PlannerActionType.SEND_LOCAL_FIX, "r",
                       target_session_id="w1",
                       message="implement divide in app.py")
    with patch.object(ex, "_run") as m:
        m.return_value = MagicMock(returncode=0, stdout="sent",
                                   stderr="")
        r = ex.execute(pa, TaskSpec.from_dict(_task_spec()))
    assert r.ok
    assert r.new_state == ProjectState.WORKER_RETRYING


def test_local_fix_budget_exceeded(tmp_path):
    ex, store = _exec(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    task.budgets["max_local_fixes"] = 1
    pa = PlannerAction("A3", "T", PlannerActionType.SEND_LOCAL_FIX, "r",
                       target_session_id="w1", message="fix")
    with patch.object(ex, "_run") as m:
        m.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ex.execute(pa, task)
        r = ex.execute(PlannerAction("A4", "T",
                      PlannerActionType.SEND_LOCAL_FIX, "r",
                      target_session_id="w1", message="fix2"), task)
    assert not r.ok
    assert r.new_state == ProjectState.HUMAN


def test_local_fix_rejects_shell_message(tmp_path):
    ex, store = _exec(tmp_path)
    pa = PlannerAction("A5", "T", PlannerActionType.SEND_LOCAL_FIX, "r",
                       target_session_id="w1", message="rm -rf / && echo")
    r = ex.execute(pa, TaskSpec.from_dict(_task_spec()))
    assert not r.ok
    assert r.new_state == ProjectState.HUMAN


def test_replan_budget(tmp_path):
    ex, store = _exec(tmp_path)
    task = TaskSpec.from_dict(_task_spec())
    task.budgets["max_replans"] = 1
    pa = PlannerAction("A6", "T", PlannerActionType.REPLAN_SPAWN, "r",
                       replacement_task_spec={"objective": "redo"})
    with patch.object(ex, "_run") as m:
        m.return_value = MagicMock(returncode=0,
                                    stdout="spawned session w-new", stderr="")
        r = ex.execute(pa, task)
        assert r.ok
        assert r.new_worker_session_id == "w-new"
        # second replan exceeds budget
        r2 = ex.execute(PlannerAction("A7", "T",
                        PlannerActionType.REPLAN_SPAWN, "r",
                        replacement_task_spec={"objective": "redo2"}), task)
    assert not r2.ok
    assert r2.new_state == ProjectState.HUMAN


def test_candidate_done_to_gate(tmp_path):
    ex, store = _exec(tmp_path)
    pa = PlannerAction("A8", "T", PlannerActionType.CANDIDATE_DONE, "r")
    r = ex.execute(pa, TaskSpec.from_dict(_task_spec()))
    assert r.ok
    assert r.new_state == ProjectState.GATE_PENDING
