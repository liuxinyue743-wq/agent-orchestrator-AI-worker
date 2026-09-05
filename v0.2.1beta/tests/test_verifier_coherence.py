"""Regression (review: verifier evidence hardening):
  - diff truncation keeps BOTH ends (the old [:limit] slice dropped the
    verdict-critical tail of any large diff).
  - verdict/ac_checks coherence: a PASS contradicted by failing AC checks is
    downgraded to FAIL (incoherent PASS must never reach DONE); a FAIL with
    all ACs green and no anti-gaming flags is retried ONCE, then HUMAN.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import FakeAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import (AcCheck, ProjectState, TaskSpec,
                                        VerifierResult)
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.state_store import StateStore
from loopcore.worktree import _head_tail
from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


def test_head_tail_keeps_both_ends():
    text = "HEAD\n" + ("x" * 10000) + "\nTAIL-VERDICT"
    out = _head_tail(text, 2000)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL-VERDICT")
    assert "chars elided" in out
    assert len(out) <= 2100
    short = "small"
    assert _head_tail(short, 2000) == short


class _ScriptedVerifier:
    """Returns queued results in order; records every input."""

    def __init__(self, *results):
        self.queue = list(results)
        self.calls = 0

    def verify(self, inp, verify_id):
        self.calls += 1
        r = self.queue.pop(0) if self.queue else self.queue_last
        self.queue_last = r
        return r


def _vr(verify_id, verdict, acs, anti=()):
    return VerifierResult(
        verify_id=verify_id, task_id="TASK-DEMO-001", verdict=verdict,
        ac_checks=[AcCheck(ac_id=a, verdict=v, note="") for a, v in acs],
        anti_gaming=[AcCheck(ac_id="anti-gaming", verdict="FAIL", note=n)
                     for n in anti],
        summary="scripted %s" % verdict)


def _make_loop(tmp_path, monkeypatch, verifier):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-xval"
    wtdir = tmp_path / "worktrees" / task.project_id / task.worker_session_id
    wtdir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wtdir), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(wtdir),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(wtdir),
                   check=True)
    (wtdir / "app.py").write_text("def divide(a, b):\n    return a / b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(wtdir), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(wtdir),
                   check=True)
    adapter = MagicMock()
    adapter.get_session_workspace.return_value = str(wtdir)
    adapter.get_worker_status.return_value = {"id": task.worker_session_id,
                                              "status": "idle"}
    task.gate_commands = ["python -c \"pass\""]
    loop = ClosedLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                      planner=FakePlannerProvider(),
                      executor=ActionExecutor("ao", "d", "r", store),
                      observer=Observer(_cfg(), state_store=store),
                      adapter=adapter, gate=IntegrationGate(store),
                      store=store, verifier=verifier)
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    loop._transition(ProjectState.GATE_PENDING, "test", "setup", {})
    loop._transition(ProjectState.VERIFIER_PENDING, "test", "setup", {})
    return loop


def test_incoherent_pass_is_downgraded(tmp_path, monkeypatch):
    verifier = _ScriptedVerifier(
        _vr("v1", "PASS", [("AC-01", "PASS"), ("AC-02", "FAIL")]))
    loop = _make_loop(tmp_path, monkeypatch, verifier)
    loop._run_verifier()
    # downgraded PASS must route into the audit pipeline, never DONE
    # (FakeAuditor+FakePlanner may already have advanced it past
    # AUDIT_PENDING within the same call — all these states prove the FAIL
    # route was taken).
    assert loop.state in (ProjectState.AUDIT_PENDING,
                          ProjectState.PLANNER_PENDING,
                          ProjectState.LOCAL_FIX_PENDING,
                          ProjectState.WORKER_RETRYING)
    assert loop.state != ProjectState.DONE
    rows = loop.store._conn.execute(
        "SELECT payload_json FROM verifications").fetchall()
    assert any("downgraded" in r[0] for r in rows)


def test_incoherent_fail_retried_once_then_human(tmp_path, monkeypatch):
    verifier = _ScriptedVerifier(
        _vr("v1", "FAIL", [("AC-01", "PASS"), ("AC-02", "PASS")]),
        _vr("v2", "FAIL", [("AC-01", "PASS"), ("AC-02", "PASS")]))
    loop = _make_loop(tmp_path, monkeypatch, verifier)
    loop._run_verifier()
    assert verifier.calls == 2            # exactly one retry, bounded
    assert loop.state == ProjectState.HUMAN


def test_incoherent_fail_retry_accepts_coherent_second(tmp_path, monkeypatch):
    verifier = _ScriptedVerifier(
        _vr("v1", "FAIL", [("AC-01", "PASS"), ("AC-02", "PASS")]),
        _vr("v2", "FAIL", [("AC-01", "PASS"), ("AC-02", "FAIL")]))
    loop = _make_loop(tmp_path, monkeypatch, verifier)
    loop._run_verifier()
    assert verifier.calls == 2
    # coherent FAIL routes back into the audit pipeline, not HUMAN / DONE
    assert loop.state in (ProjectState.AUDIT_PENDING,
                          ProjectState.PLANNER_PENDING,
                          ProjectState.LOCAL_FIX_PENDING,
                          ProjectState.WORKER_RETRYING)
    assert loop.state not in (ProjectState.HUMAN, ProjectState.DONE)
