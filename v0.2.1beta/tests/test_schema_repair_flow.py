"""Deterministic transport simulation + real Git/Gate recovery regression.

The model/AO transports are stubbed; the code under test is the production
Auditor, Planner, action executor, state store and gate. NOT a live AO proof.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from loopcore.action_executor import ActionExecutor
from loopcore.auditor import CodexCliAuditorProvider
from loopcore.closed_loop import ClosedLoop
from loopcore.event_observer import Observer
from loopcore.mission_contracts import TaskSpec, ProjectState
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import CodexCliPlannerProvider
from loopcore.state_store import StateStore
from tests.sidecar_port.test_contracts import _task_spec
from tests.sidecar_port.test_phase3 import _cfg


def test_failed_gate_repair_restart_and_new_gate_pass(tmp_path, monkeypatch):
    repo = tmp_path / "target"; repo.mkdir()
    (repo / "app.py").write_text("def divide(a,b):\n    return a*b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    test_file = repo / "tests/test_divide.py"
    test_file.write_text(
        "import pytest\nfrom app import divide\n"
        "def test_division(): assert divide(6,3) == 2\n"
        "def test_zero():\n    with pytest.raises(ValueError): divide(1,0)\n",
        encoding="utf-8")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "fixture"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    test_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()
    task = TaskSpec.from_dict(_task_spec()); task.worker_session_id = "simulated-worker-1"
    database = tmp_path / "state.db"
    calls = {"audits": [], "plans": [], "sends": []}

    def wire(store):
        auditor = CodexCliAuditorProvider()
        planner = CodexCliPlannerProvider()
        adapter = MagicMock()
        adapter.get_worker_conversation.return_value = {"activities": []}
        executor = ActionExecutor("ao-not-executed", None, None, store)
        def audit_call(bundle, audit_id):
            calls["audits"].append(bundle.test_output)
            decision = "LOCAL_FIX" if bundle.failed_criteria else "PASS"
            return {
                "audit_id": audit_id, "task_id": task.task_id,
                "decision": decision, "evidence": [{"type": "gate", "summary": bundle.test_output[:300]}],
                "diagnosis": "real gate output checked", "confidence": 0.9,
                "failed_criteria": list(bundle.failed_criteria),
                "recommended_action": "Correct divide in app.py; preserve tests.",
            }
        def planner_call(audit, _task, action_id, **kwargs):
            calls["plans"].append(audit.decision)
            return {
                "action_id": action_id, "task_id": task.task_id,
                "action": "SEND_LOCAL_FIX" if audit.decision == "LOCAL_FIX" else "CANDIDATE_DONE",
                "reason": "follow validated audit",
                "target_session_id": task.worker_session_id,
                "message": "Correct divide in app.py; preserve tests.",
                "replacement_task_spec": None,
            }
        def simulate_worker_feedback(args, **kwargs):
            assert args[:3] == ["send", "--session", task.worker_session_id]
            calls["sends"].append(args)
            # Explicitly the simulated worker transport, NOT a real live run.
            (repo / "app.py").write_text(
                "def divide(a,b):\n    if b == 0:\n        raise ValueError('zero')\n    return a/b\n",
                encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="simulated delivery", stderr="")
        monkeypatch.setattr(auditor, "_call", audit_call)
        monkeypatch.setattr(planner, "_call", planner_call)
        monkeypatch.setattr(executor, "_run", simulate_worker_feedback)
        loop = ClosedLoop(task, _cfg(), auditor=auditor, planner=planner,
                          executor=executor, observer=Observer(_cfg(), state_store=store),
                          adapter=adapter, gate=IntegrationGate(store), store=store)
        monkeypatch.setattr(loop, "_worktree_path", lambda: str(repo))
        monkeypatch.setattr(loop, "_base_commit", lambda: baseline)
        monkeypatch.setattr(loop, "_worker_status", lambda: {"activity": {"state": "idle"}})
        return loop

    store = StateStore(database); store.record_task(task.task_id, task.to_dict())
    loop = wire(store)
    loop._transition(ProjectState.WORKER_RUNNING, "fixture", "simulated dispatch", {})
    loop._transition(ProjectState.GATE_PENDING, "fixture", "test submitted implementation", {})
    loop._run_gate()
    assert loop.state == ProjectState.WORKER_RETRYING
    assert len(calls["sends"]) == 1
    assert calls["plans"] == ["LOCAL_FIX"]
    store.close()

    # Reopening is supported recovery, not editing/resetting the stored state.
    store = StateStore(database); loop = wire(store)
    assert loop.state == ProjectState.WORKER_RETRYING
    loop.step()
    assert loop.state == ProjectState.DONE
    assert calls["plans"] == ["LOCAL_FIX", "PASS"]
    assert len(calls["audits"]) == 2 and len(calls["sends"]) == 1
    assert "failed" in calls["audits"][0]
    assert "passed" in calls["audits"][1]
    assert hashlib.sha256(test_file.read_bytes()).hexdigest() == test_hash
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip() == baseline
    loop.step()  # terminal state: no duplicate actions
    assert len(calls["sends"]) == 1
    store.close()
