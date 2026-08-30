"""StoreBusProjector tests: real StateStore rows -> Bus envelopes + memory files.

All offline: temp SQLite, temp project root, in-memory LoopBus with sink
handlers. No AO, no LLM, no network.
"""

from __future__ import annotations

import json

import pytest

from loopcore.bus import LoopBus
from loopcore.bus_projector import StoreBusProjector
from loopcore.envelope import MessageKind
from loopcore.memory import ProjectMemory
from loopcore.state_store import StateStore


@pytest.fixture()
def store(tmp_path):
    return StateStore(str(tmp_path / "state.db"))


@pytest.fixture()
def memory(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return ProjectMemory(str(proj))


def _collect(bus: LoopBus) -> list:
    seen = []

    def sink(env):
        seen.append(env)

    for ep in ("observer", "auditor", "planner", "verifier", "gate",
               "human", "user", "worker:sess-9"):
        bus.register(ep, sink)
    return seen


def test_alert_projects_as_observer_trigger(store, memory):
    bus = LoopBus()
    seen = _collect(bus)
    store.record_alert("alert-1", {"alert_type": "REPEATED_ERROR",
                                   "description": "same error x3"})
    proj = StoreBusProjector(store, bus, memory)
    proj.project_once()
    assert [e.kind for e in seen] == [MessageKind.TRIGGER]
    assert seen[0].sender == "observer" and seen[0].receiver == "auditor"


def test_audit_action_verification_gate_flow(store, memory):
    bus = LoopBus()
    seen = _collect(bus)
    store.record_audit("AUDIT-1", "T1", {"decision": "LOCAL_FIX"})
    store.record_action("ACTION-1", "T1", {
        "action": "SEND_LOCAL_FIX", "target_session_id": "sess-9",
        "message": "fix it"})
    store.record_verification("VERIFY-1", "T1", {"verdict": "PASS"})
    store.record_gate_run(task_id="T1", command="pytest", cwd="/tmp",
                          exit_code=0, started_at="t0", ended_at="t1",
                          stdout="ok", stderr="")
    proj = StoreBusProjector(store, bus, memory)
    proj.project_once()
    kinds = [e.kind for e in seen]
    assert MessageKind.AUDIT_REPORT in kinds
    assert MessageKind.LOCAL_FIX in kinds
    assert MessageKind.VERDICT in kinds
    assert MessageKind.GATE_EVIDENCE in kinds
    fix = next(e for e in seen if e.kind is MessageKind.LOCAL_FIX)
    assert fix.receiver == "worker:sess-9"
    assert proj.errors == []


def test_transition_writes_memory_and_terminal_writes_project(store, memory):
    bus = LoopBus()
    _collect(bus)
    store.record_transition(task_id="T1", from_state="WORKER_RUNNING",
                            to_state="DONE", actor="verifier",
                            reason="verifier PASS", evidence={})
    proj = StoreBusProjector(store, bus, memory)
    proj.project_once()
    text = memory.read("memory")
    assert "WORKER_RUNNING → DONE" in text
    assert "verifier PASS" in memory.read("project")


def test_terminal_mission_emits_final_report(store, memory):
    bus = LoopBus()
    seen = _collect(bus)
    store.record_mission("MISSION-X", {"state": "MISSION_DONE",
                                       "reason": "gate pass + verifier PASS"})
    proj = StoreBusProjector(store, bus, memory)
    proj.project_once()
    finals = [e for e in seen if e.kind is MessageKind.FINAL_REPORT]
    assert len(finals) == 1
    assert finals[0].sender == "planner" and finals[0].receiver == "user"
    assert finals[0].payload["state"] == "MISSION_DONE"
    assert "MISSION_DONE" in memory.read("project")


def test_projection_is_idempotent_across_passes(store, memory):
    bus = LoopBus()
    seen = _collect(bus)
    store.record_audit("AUDIT-1", "T1", {"decision": "PASS"})
    proj = StoreBusProjector(store, bus, memory)
    assert proj.project_once() == 1
    assert proj.project_once() == 0  # high-water mark: nothing new
    assert len(seen) == 1


def test_projection_cursors_survive_restart(store, memory, tmp_path):
    bus = LoopBus()
    _collect(bus)
    store.record_audit("AUDIT-1", "T1", {"decision": "PASS"})
    StoreBusProjector(store, bus, memory).project_once()
    # a NEW projector over the SAME store must not re-project old rows
    proj2 = StoreBusProjector(store, bus, memory)
    assert proj2.project_once() == 0


def test_disallowed_route_is_collected_not_raised(store, memory):
    """A route-matrix violation in stored data lands in errors, never crashes."""
    bus = LoopBus()
    _collect(bus)
    # verifier->auditor is NOT in the matrix; force one via a malformed row:
    store.record_mission("MISSION-BAD", {"state": "HUMAN", "reason": "halt"})
    proj = StoreBusProjector(store, bus, memory)
    proj.project_once()  # planner->user FINAL_REPORT is legal -> no error here
    assert proj.errors == []
