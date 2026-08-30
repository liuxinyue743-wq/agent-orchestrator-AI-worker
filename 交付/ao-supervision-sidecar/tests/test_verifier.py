"""Verifier flow tests: gate pass -> independent verification -> DONE/loop-back.

Uses fake providers + temp SQLite; no real AO/Claude. The key property under
test: the deterministic gate alone NEVER produces DONE — an independent
verifier PASS is required, and a verifier FAIL re-enters the audit->planner
loop with the verifier findings as evidence.
"""
from unittest.mock import MagicMock, patch

from src.auditor import FakeAuditorProvider
from src.closed_loop import ClosedLoop
from src.contracts import (AuditDecision, AuditResult, AuditEvidence,
                           PlannerAction, PlannerActionType, ProjectState,
                           VerifierResult, AcCheck)
from src.integration_gate import IntegrationGate
from src.observer import Observer
from src.planner_adapter import FakePlannerProvider
from src.state_store import StateStore
from src.verifier import FakeVerifierProvider, VerifierInput
from tests.test_contracts import _task_spec
from tests.test_budgets import _cfg


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

    _run_gate/_run_verifier resolve the worktree from AO_DATA_DIR and compute
    real changed paths / path violations against it, so the test needs a
    genuine git repo with an app.py edit.
    """
    import os
    import subprocess
    from src.contracts import TaskSpec
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
    os.environ["AO_DATA_DIR"] = str(data_dir)
    store = StateStore(tmp_path / "cl.db")
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w1"
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    adapter.get_worker_status.return_value = {"id": "w1", "status": "idle"}
    gate = IntegrationGate(store)
    if fake_gate_run_ok:
        gate.run = MagicMock(return_value=MagicMock(
            ok=True, results=[{"command": "pytest", "stdout": "2 passed",
                               "stderr": ""}],
            evidence=lambda: [{"type": "integration_gate",
                               "summary": "pass", "reference": "exit=0"}]))
    from src.action_executor import ActionExecutor
    ex = ActionExecutor("ao", "d", "r", store)
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(), executor=ex, observer=obs,
                      adapter=adapter, gate=gate, store=store,
                      verifier=verifier)
    return loop, task, store


def test_gate_pass_then_verifier_pass_reaches_done(tmp_path):
    """Gate pass alone is NOT DONE; verifier PASS is required to finish."""
    v = _ScriptedVerifier("PASS")
    loop, task, store = _loop(tmp_path, v)
    loop._transition(ProjectState.WORKER_RUNNING, "t", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "t", "setup", {})
    loop._run_gate()
    assert loop.state == ProjectState.DONE
    # the verifier saw real gate output
    assert "2 passed" in v.inputs[0].gate_output


def test_verifier_fail_reenters_audit_planner(tmp_path):
    """Verifier FAIL -> AUDIT_PENDING -> planner gets verifier evidence."""
    v = _ScriptedVerifier("FAIL")
    loop, task, store = _loop(tmp_path, v)
    loop._transition(ProjectState.WORKER_RUNNING, "t", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "t", "setup", {})
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
    loop._run_gate()
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
