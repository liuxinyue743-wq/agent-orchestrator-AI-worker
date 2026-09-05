"""Task-gate completion and historical Verifier recovery tests.

Uses fake providers + temp SQLite; no real AO/model call. The key property under
test: a new deterministic Gate PASS reaches DONE without a task Verifier,
while historical VERIFIER_PENDING rows retain their old PASS/FAIL behavior.
"""
from unittest.mock import MagicMock, patch

from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.mission_contracts import (AuditDecision, AuditResult, AuditEvidence,
                           PlannerAction, PlannerActionType, ProjectState,
                           VerifierResult, AcCheck)
from loopcore.mission_gate import IntegrationGate
from loopcore.event_observer import Observer
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.verifier import FakeVerifierProvider, VerifierInput
from tests.sidecar_port.test_contracts import _task_spec
from tests.sidecar_port.test_budgets import _cfg


class _ScriptedVerifier:
    """Verifier stub returning a preset verdict."""
    def __init__(self, verdict="PASS"):
        self.verdict = verdict
        self.inputs = []

    def verify(self, inp, verify_id):
        self.inputs.append(inp)
        return VerifierResult(
            verify_id=verify_id, task_id=inp.task_spec.get("task_id", ""),
            verdict=self.verdict,
            ac_checks=[AcCheck(ac_id="AC-01", verdict=self.verdict)],
            anti_gaming=[],
            summary="scripted %s" % self.verdict)


def _loop(tmp_path, verifier, *, fake_gate_run_ok=True):
    """Build a ClosedLoop with a REAL minimal git worktree under tmp_path.

    _run_gate/_run_verifier resolve the worktree through the AO adapter and
    compute real changed paths / path violations against it, so the test needs
    a genuine git repo with an app.py edit.
    """
    import subprocess
    from loopcore.mission_contracts import TaskSpec
    data_dir = tmp_path / "ao-data"
    wt = data_dir / "worktrees" / "closed-loop-demo" / "w1"
    wt.mkdir(parents=True)
    def _git(*a):
        subprocess.run(["git", "-C", str(wt), *a], capture_output=True,
                       text=True)
    _git("init", "-q")
    _git("config", "user.name", "t"); _git("config", "user.email", "t@t")
    (wt / "app.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
    _git("add", "-A"); _git("commit", "-q", "-m", "init")
    (wt / "app.py").write_text(
        "def add(a,b): return a+b\ndef divide(a,b):\n"
        "    if b==0: raise ValueError\n    return a/b\n", encoding="utf-8")
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w1"
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_session_workspace.return_value = str(wt)
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": "w1", "status": "idle"}
    gate = IntegrationGate(store)
    gate.run = MagicMock(return_value=MagicMock(
        ok=fake_gate_run_ok,
        results=[{"command": "pytest",
                  "stdout": "2 passed" if fake_gate_run_ok else "1 failed",
                  "stderr": ""}],
        evidence=lambda: [{"type": "integration_gate",
                           "summary": "pass" if fake_gate_run_ok else "fail",
                           "reference": "exit=%d" %
                           (0 if fake_gate_run_ok else 1)}]))
    from loopcore.action_executor import ActionExecutor
    ex = ActionExecutor("ao", "d", "r", store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex, observer=obs,
                      adapter=adapter, gate=gate, store=store,
                      verifier=verifier)
    return loop, task, store


def test_gate_pass_reaches_done_without_task_verifier(tmp_path):
    v = _ScriptedVerifier("PASS")
    loop, task, store = _loop(tmp_path, v)
    loop._transition(ProjectState.WORKER_RUNNING, "t", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "t", "setup", {})
    loop._run_gate()
    assert loop.state == ProjectState.DONE
    assert v.inputs == []
    assert store._conn.execute(
        "SELECT count(*) FROM verifications WHERE task_id=?",
        (task.task_id,)).fetchone()[0] == 0
    transitions = store._conn.execute(
        "SELECT to_state FROM state_transitions WHERE task_id=? ORDER BY id",
        (task.task_id,)).fetchall()
    assert transitions[-1] == (ProjectState.DONE,)
    assert (ProjectState.VERIFIER_PENDING,) not in transitions


def test_gate_fail_does_not_finish_or_call_task_verifier(tmp_path):
    v = _ScriptedVerifier("PASS")
    loop, task, store = _loop(tmp_path, v, fake_gate_run_ok=False)
    loop._transition(ProjectState.WORKER_RUNNING, "t", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "t", "setup", {})
    audit = AuditResult("A-GATE-FAIL", task.task_id, AuditDecision.HUMAN,
                        [AuditEvidence("test_failure", "gate failed")],
                        "gate failed", 1.0, ["AC-01"])
    loop.auditor.audit = MagicMock(return_value=audit)
    loop.planner.plan = MagicMock(return_value=PlannerAction(
        "ACT-GATE-FAIL", task.task_id, PlannerActionType.HUMAN,
        reason="gate failure requires human"))
    loop.executor.execute = MagicMock(return_value=MagicMock(
        ok=True, new_state=ProjectState.HUMAN,
        new_worker_session_id=None, detail="halted"))

    loop._run_gate()

    assert loop.state == ProjectState.HUMAN
    assert loop.state != ProjectState.DONE
    assert v.inputs == []
    loop.auditor.audit.assert_called_once()
    assert store._conn.execute(
        "SELECT count(*) FROM state_transitions "
        "WHERE task_id=? AND from_state=? AND to_state=? AND actor=?",
        (task.task_id, ProjectState.GATE_PENDING,
         ProjectState.AUDIT_PENDING, "integration_gate")).fetchone()[0] == 1


def test_historical_verifier_fail_reenters_audit_planner(tmp_path):
    """Historical VERIFIER_PENDING FAIL keeps its audit/planner semantics."""
    v = _ScriptedVerifier("FAIL")
    loop, task, store = _loop(tmp_path, v)
    loop._transition(ProjectState.WORKER_RUNNING, "t", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "t", "setup", {})
    loop._transition(ProjectState.VERIFIER_PENDING, "t", "historical", {})
    loop.auditor = MagicMock(return_value=None)
    audit = AuditResult("A-V1", task.task_id, AuditDecision.LOCAL_FIX,
                        [AuditEvidence("verifier_fail", "AC-01 FAIL")],
                        "d", 0.9, ["AC-01"])
    loop.auditor.audit = MagicMock(return_value=audit)
    loop.planner = MagicMock()
    loop.planner.plan = MagicMock(return_value=PlannerAction(
        "ACT-V1", task.task_id, PlannerActionType.SEND_LOCAL_FIX,
        reason="fix verifier findings", target_session_id="w1",
        message="fix AC-01"))
    loop.executor.execute = MagicMock(return_value=MagicMock(
        ok=True, new_state=ProjectState.WORKER_RETRYING,
        new_worker_session_id=None, detail="sent"))
    loop._run_verifier()
    assert loop.state != ProjectState.DONE
    # planner was consulted after the verifier-driven audit
    loop.planner.plan.assert_called_once()
    # the audit bundle carried the failed AC from the verifier
    args = loop.auditor.audit.call_args[0][0]
    assert "AC-01" in args.failed_criteria


def test_fake_verifier_red_flags_fail(tmp_path):
    """FakeVerifierProvider: deterministic path violations force FAIL."""
    fake = FakeVerifierProvider()
    inp = VerifierInput(
        task_spec={"task_id": "T1", "acceptance_criteria": [
            {"id": "AC-01", "description": "works"}]},
        diff="diff --git a/app.py", gate_output="2 passed",
        changed_paths=["app.py", "tests/test_x.py"],
        deterministic_findings=["path violation (forbidden): tests/test_x.py"])
    res = fake.verify(inp, "V1")
    assert res.verdict == "FAIL"
    assert res.gaming_flags()


def test_fake_verifier_clean_pass(tmp_path):
    fake = FakeVerifierProvider()
    inp = VerifierInput(
        task_spec={"task_id": "T1", "acceptance_criteria": [
            {"id": "AC-01", "description": "works"}]},
        diff="diff --git a/app.py", gate_output="2 passed",
        changed_paths=["app.py"], deterministic_findings=[])
    res = fake.verify(inp, "V2")
    assert res.verdict == "PASS"
    assert res.failed_acs() == []


def test_verifier_result_roundtrip():
    d = {"verify_id": "V9", "task_id": "T9", "verdict": "FAIL",
         "ac_checks": [{"ac_id": "AC-01", "verdict": "FAIL", "note": "bad"}],
         "anti_gaming": [{"ac_id": "tests-untouched", "verdict": "PASS"}],
         "summary": "s"}
    r = VerifierResult.from_dict(d)
    assert r.failed_acs() == ["AC-01"]
    assert r.gaming_flags() == []
    assert r.to_dict()["verify_id"] == "V9"
