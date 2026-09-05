"""Offline tests for LoopBus: dedup inbox, budgets, HUMAN fallback."""

from __future__ import annotations

import pytest

from loopcore.bus import (
    BudgetExceeded,
    BusConfig,
    BusError,
    LoopBus,
    RouteNotRegistered,
)
from loopcore.envelope import Envelope, MessageKind, issue_fingerprint


@pytest.fixture
def bus_setup():
    bus = LoopBus(BusConfig(max_hops_per_thread=6, max_audits_per_thread=2,
                            overall_timeout_seconds=600))
    boxes: dict[str, list[Envelope]] = {
        "planner": [], "worker:w-1": [], "worker:w-2": [],
        "auditor": [], "verifier": [], "observer": [], "human": [],
    }
    for ep, box in boxes.items():
        bus.register(ep, box.append)
    return bus, boxes


def env(sender, receiver, kind, thread="T", payload=None, hop=0):
    return Envelope(sender=sender, receiver=receiver, kind=kind,
                    thread_id=thread, payload=payload or {}, hop=hop)


class TestDispatch:
    def test_delivery_increments_hop(self, bus_setup):
        bus, boxes = bus_setup
        bus.submit(env("planner", "worker:w-1", MessageKind.TASK_DISPATCH))
        assert boxes["worker:w-1"][0].hop == 1

    def test_unregistered_receiver_fails_closed(self, bus_setup):
        bus, _ = bus_setup
        with pytest.raises(RouteNotRegistered):
            bus.submit(env("planner", "worker:w-9", MessageKind.TASK_DISPATCH))

    def test_identical_replay_rejected(self, bus_setup):
        bus, _ = bus_setup
        e = env("planner", "worker:w-1", MessageKind.TASK_DISPATCH, payload={"g": 1})
        bus.submit(e)
        with pytest.raises(BusError, match="duplicate idempotency key"):
            bus.submit(e)


class TestDualChannelDedup:
    def test_worker_and_auditor_merge_into_one_thread(self, bus_setup):
        bus, boxes = bus_setup
        fp = issue_fingerprint("TASK-1", "pytest failed: test_divide", "worker:")
        bus.submit(env("worker:w-1", "planner", MessageKind.BLOCKER_REPORT,
                       thread="M1", payload={"issueFingerprint": fp, "evidence": "w"}))
        merged = bus.submit(env("auditor", "planner", MessageKind.ESCALATION,
                                thread="M2", payload={"issueFingerprint": fp, "evidence": "a"}))
        assert merged.thread_id == "M1"
        assert merged.payload["mergedInto"] == "M1"
        assert merged.payload["reporters"] == ["worker:w-1", "auditor"]

    def test_dedup_requires_fingerprint(self, bus_setup):
        bus, _ = bus_setup
        with pytest.raises(BusError, match="issueFingerprint"):
            bus.submit(env("worker:w-1", "planner", MessageKind.BLOCKER_REPORT))

    def test_verdict_recorded_once(self, bus_setup):
        bus, _ = bus_setup
        fp = issue_fingerprint("TASK-1", "boom", "worker:")
        bus.submit(env("worker:w-1", "planner", MessageKind.BLOCKER_REPORT,
                       payload={"issueFingerprint": fp}))
        record = bus.resolve_issue(fp, {"decision": "LOCAL_FIX"})
        assert record.verdict["decision"] == "LOCAL_FIX"
        with pytest.raises(BusError, match="already adjudicated"):
            bus.resolve_issue(fp, {"decision": "PASS"})

    def test_unknown_fingerprint_rejected(self, bus_setup):
        bus, _ = bus_setup
        with pytest.raises(BusError, match="unknown issue fingerprint"):
            bus.resolve_issue("worker::T:none", {"decision": "PASS"})


class TestBudgets:
    def test_thread_hop_budget_routes_to_human(self, bus_setup):
        bus, boxes = bus_setup
        for i in range(6):
            bus.submit(env("planner", "worker:w-1", MessageKind.TASK_DISPATCH,
                           thread="M1", payload={"i": i}))
        with pytest.raises(BudgetExceeded):
            bus.submit(env("planner", "worker:w-1", MessageKind.TASK_DISPATCH,
                           thread="M1", payload={"i": 6}))
        assert boxes["human"][-1].payload["reason"] == "max hops per thread exceeded"

    def test_single_message_hop_limit(self, bus_setup):
        bus, boxes = bus_setup
        with pytest.raises(BudgetExceeded):
            bus.submit(env("planner", "worker:w-1", MessageKind.LOCAL_FIX,
                           thread="M2", hop=6))
        assert boxes["human"][-1].payload["reason"] == "single message hop limit exceeded"

    def test_audit_budget_routes_to_human(self, bus_setup):
        bus, boxes = bus_setup
        for i in range(2):
            bus.submit(env("planner", "auditor", MessageKind.AUDIT_REQUEST,
                           thread="M3", payload={"cycle": i}))
        with pytest.raises(BudgetExceeded):
            bus.submit(env("planner", "auditor", MessageKind.AUDIT_REQUEST,
                           thread="M3", payload={"cycle": 2}))
        assert boxes["human"][-1].payload["reason"] == "max audits per thread exceeded"

    def test_config_requires_positive_values(self):
        with pytest.raises(ValueError):
            BusConfig(max_hops_per_thread=0)
        with pytest.raises(ValueError):
            BusConfig(overall_timeout_seconds=-1)

    def test_threads_are_isolated(self, bus_setup):
        bus, _ = bus_setup
        for i in range(6):
            bus.submit(env("planner", "worker:w-1", MessageKind.TASK_DISPATCH,
                           thread="A", payload={"i": i}))
        # a different thread is unaffected
        bus.submit(env("planner", "worker:w-2", MessageKind.TASK_DISPATCH,
                       thread="B", payload={"i": 0}))
