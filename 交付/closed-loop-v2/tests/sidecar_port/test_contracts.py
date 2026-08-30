"""Phase 1 tests: TaskSpec / AuditResult / PlannerAction validation + state machine."""
from loopcore.mission_contracts import (
    TaskSpec, AuditResult, PlannerAction, AuditEvidence,
    AuditDecision, PlannerActionType, ProjectState,
    is_legal_transition, validate_task_spec, validate_audit_result,
    validate_planner_action,
)


def _task_spec():
    return {
        "task_id": "TASK-DEMO-001", "project_id": "closed-loop-demo",
        "objective": "实现 divide 函数并通过规定测试",
        "allowed_paths": ["app.py"],
        "forbidden_paths": ["tests/**", ".git/**"],
        "acceptance_criteria": [
            {"id": "AC-01", "description": "divide(6,3)==2"},
            {"id": "AC-02", "description": "divide(1,0) raises ValueError"},
        ],
        "required_evidence": ["git diff", "pytest exit code"],
        "gate_commands": ["python -m pytest -q"],
        "budgets": {"max_local_fixes": 2, "max_replans": 1,
                    "max_same_alerts": 1, "max_runtime_seconds": 1800},
    }


def test_task_spec_validation():
    ok, msg = validate_task_spec(_task_spec())
    assert ok, msg
    bad = _task_spec()
    del bad["budgets"]
    ok, msg = validate_task_spec(bad)
    assert not ok


def test_task_spec_roundtrip():
    ts = TaskSpec.from_dict(_task_spec())
    ok, msg = ts.validate()
    assert ok, msg
    assert ts.budgets["max_local_fixes"] == 2


def test_audit_result_validation():
    ar = {
        "audit_id": "A1", "task_id": "T1", "decision": "LOCAL_FIX",
        "failed_criteria": ["AC-01"],
        "evidence": [{"type": "test_failure", "summary": "no divide"}],
        "diagnosis": "not implemented", "confidence": 0.9,
    }
    ok, msg = validate_audit_result(ar)
    assert ok, msg
    bad = dict(ar, decision="MAYBE")
    assert not validate_audit_result(bad)[0]
    bad2 = dict(ar, evidence=[])
    assert not validate_audit_result(bad2)[0]


def test_planner_action_validation():
    pa = {"action_id": "A1", "task_id": "T1", "action": "SEND_LOCAL_FIX",
          "target_session_id": "s1", "message": "fix app.py",
          "replacement_task_spec": None, "reason": "local fix ok"}
    ok, msg = validate_planner_action(pa)
    assert ok, msg
    bad = dict(pa, action="DELETE_EVERYTHING")
    assert not validate_planner_action(bad)[0]


def test_state_machine_valid_transitions():
    assert is_legal_transition(ProjectState.TASK_READY,
                               ProjectState.WORKER_RUNNING)
    assert is_legal_transition(ProjectState.WORKER_RUNNING,
                               ProjectState.AUDIT_PENDING)
    assert is_legal_transition(ProjectState.AUDIT_PENDING,
                               ProjectState.PLANNER_PENDING)
    assert is_legal_transition(ProjectState.PLANNER_PENDING,
                               ProjectState.LOCAL_FIX_PENDING)
    # DONE now REQUIRES an independent verifier PASS — the gate routes to
    # VERIFIER_PENDING, never straight to DONE.
    assert is_legal_transition(ProjectState.GATE_PENDING,
                               ProjectState.VERIFIER_PENDING)
    assert is_legal_transition(ProjectState.VERIFIER_PENDING,
                               ProjectState.DONE)
    assert is_legal_transition(ProjectState.VERIFIER_PENDING,
                               ProjectState.AUDIT_PENDING)
    assert not is_legal_transition(ProjectState.GATE_PENDING,
                                   ProjectState.DONE)
    assert is_legal_transition(ProjectState.PLANNER_PENDING,
                               ProjectState.HUMAN)


def test_state_machine_rejects_invalid_transition():
    # cannot jump straight from TASK_READY to DONE
    assert not is_legal_transition(ProjectState.TASK_READY,
                                   ProjectState.DONE)
    # DONE is terminal
    assert not is_legal_transition(ProjectState.DONE,
                                   ProjectState.WORKER_RUNNING)
    # cannot go from WORKER_RUNNING to DONE directly
    assert not is_legal_transition(ProjectState.WORKER_RUNNING,
                                   ProjectState.DONE)
