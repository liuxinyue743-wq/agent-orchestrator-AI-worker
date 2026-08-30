"""LOOP_ERROR escalation: consecutive step() exceptions must halt to HUMAN
after MAX_CONSECUTIVE_LOOP_ERRORS, and a successful tick must reset the
streak. Unbounded retry re-invokes an LLM role every poll (real-run cost:
up to max_runtime_seconds / poll_interval model calls per fault)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock

from loopcore.auditor import FakeAuditorProvider
from loopcore.event_observer import Observer
from loopcore.mission_gate import IntegrationGate
from loopcore.planner_adapter import FakePlannerProvider
from loopcore.verifier import FakeVerifierProvider
from loopcore.action_executor import ActionExecutor
from loopcore.closed_loop import ClosedLoop, ProjectState
from loopcore.mission import MissionController
from loopcore.state_store import StateStore
from loopcore.mission_contracts import TaskSpec

from tests.sidecar_port.test_phase3 import _cfg
from tests.sidecar_port.test_contracts import _task_spec


class _BoomLoop(ClosedLoop):
    """ClosedLoop whose _step_impl raises while fail_remaining == 0."""
    fail_remaining = 0  # 0 = fail forever

    def _step_impl(self, injected_events=None):
        if self.fail_remaining == 0:
            raise RuntimeError("persistent git failure")
        self.fail_remaining -= 1
        return {"state": self.state, "acted": False}


def _make_loop(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "cl.db"))
    task = TaskSpec.from_dict(_task_spec())
    task.worker_session_id = "w-err"
    monkeypatch.setenv("AO_DATA_DIR", str(tmp_path))
    obs = Observer(_cfg(), state_store=store)
    adapter = MagicMock()
    adapter.get_recent_events.return_value = []
    ex = ActionExecutor("ao", "d", "r", store)
    loop = _BoomLoop(task=task, cfg=_cfg(), auditor=FakeAuditorProvider(),
                     planner=FakePlannerProvider(), executor=ex, observer=obs,
                     adapter=adapter, gate=IntegrationGate(store),
                     store=store, verifier=FakeVerifierProvider())
    loop._transition(ProjectState.WORKER_RUNNING, "test", "setup", {})
    return loop


def test_consecutive_errors_halt_to_human(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, monkeypatch)
    r1 = loop.step()          # error 1: retry
    r2 = loop.step()          # error 2: retry
    assert r1["state"] != "HUMAN" and r2["state"] != "HUMAN"
    r3 = loop.step()          # error 3: halt
    assert r3["state"] == "HUMAN", "3rd consecutive error must halt to HUMAN"


def test_success_resets_streak(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, monkeypatch)
    loop.step()               # error 1
    loop.step()               # error 2
    loop.fail_remaining = 99  # succeed from here on
    ok = loop.step()
    assert "error" not in ok  # success -> streak reset
    loop.fail_remaining = 0   # fail again: streak restarts at 1
    r = loop.step()
    assert r["state"] != "HUMAN", "streak must reset after a successful tick"


def test_mission_controller_escalation(tmp_path):
    """MissionController.step boundary: same N-strikes semantics."""
    store = StateStore(str(tmp_path / "m.db"))
    mc = MissionController.__new__(MissionController)
    mc.mission = MagicMock()
    mc.mission.mission_id = "M-ERR"
    mc.store = store
    # `state` is a read-through property backed by the store; seed it there.
    store.record_mission("M-ERR", {"state": "MISSION_RUNNING", "reason": "",
                                    "mission": {}, "plan": None})

    def _boom():
        raise RuntimeError("mission-level persistent fault")

    mc._step_impl = _boom
    r1, r2 = mc.step(), mc.step()
    assert r1["state"] != "HUMAN" and r2["state"] != "HUMAN"
    r3 = mc.step()
    assert r3["state"] == "HUMAN"
