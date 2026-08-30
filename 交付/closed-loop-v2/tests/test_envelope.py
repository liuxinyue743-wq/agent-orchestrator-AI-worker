"""Offline tests for the Loop Bus envelope and routing matrix.

Matrix rules (owner-ruled 2026-08-30): Auditor->Verifier and Observer->Verifier
are one-way; all other role pairs are bidirectional.
"""

from __future__ import annotations

import pytest

from loopcore.envelope import (
    Envelope,
    MessageKind,
    Role,
    issue_fingerprint,
)


def make(sender, receiver, kind, thread="t1", payload=None, hop=0):
    return Envelope(
        sender=sender,
        receiver=receiver,
        kind=kind,
        thread_id=thread,
        payload=payload or {},
        hop=hop,
    )


class TestRoutingMatrix:
    @pytest.mark.parametrize(
        "sender,receiver,kind",
        [
            ("planner", "worker:w-1", MessageKind.TASK_DISPATCH),
            ("worker:w-1", "planner", MessageKind.BLOCKER_REPORT),
            ("worker:w-1", "planner", MessageKind.CHECKER_REQUEST),
            ("planner", "auditor", MessageKind.AUDIT_REQUEST),
            ("auditor", "planner", MessageKind.AUDIT_REPORT),
            ("auditor", "planner", MessageKind.ESCALATION),
            ("planner", "verifier", MessageKind.PV_TASK),
            ("verifier", "planner", MessageKind.PV_RESULT),
            ("verifier", "planner", MessageKind.VERDICT),
            ("planner", "observer", MessageKind.WATCH_DIRECTIVE),
            ("observer", "planner", MessageKind.RISK_SIGNAL),
            ("planner", "gate", MessageKind.GATE_RUN),
            ("gate", "planner", MessageKind.GATE_EVIDENCE),
            ("worker:w-1", "auditor", MessageKind.STATUS_CLAIM),
            ("auditor", "worker:w-1", MessageKind.LOCAL_FIX),
            ("auditor", "worker:w-1", MessageKind.AUDIT_QUERY),
            ("worker:w-1", "verifier", MessageKind.VERIFY_REQUEST),
            ("verifier", "worker:w-1", MessageKind.FIX_REQUEST),
            ("worker:w-1", "observer", MessageKind.STATUS_NOTE),
            ("observer", "worker:w-1", MessageKind.STALL_NOTICE),
            ("auditor", "observer", MessageKind.FOCUS_WATCH),
            ("observer", "auditor", MessageKind.TRIGGER),
            ("auditor", "verifier", MessageKind.AUDIT_VERIFY_REQUEST),
            ("observer", "verifier", MessageKind.TRIGGER_VERIFY),
            ("planner", "bus", MessageKind.MEMORY_UPDATE),
            ("planner", "user", MessageKind.FINAL_REPORT),
        ],
    )
    def test_allowed_routes(self, sender, receiver, kind):
        make(sender, receiver, kind)  # must not raise

    @pytest.mark.parametrize(
        "sender,receiver,kind",
        [
            # one-way enforcement: reverse direction must fail
            ("verifier", "auditor", MessageKind.VERDICT),
            ("verifier", "auditor", MessageKind.PV_RESULT),
            ("verifier", "observer", MessageKind.VERDICT),
            ("verifier", "observer", MessageKind.FIX_REQUEST),
            # workers never command other roles
            ("worker:w-1", "verifier", MessageKind.TASK_DISPATCH),
            ("worker:w-2", "worker:w-1", MessageKind.TASK_DISPATCH),
            # programs never dispatch semantic tasks
            ("observer", "worker:w-1", MessageKind.TASK_DISPATCH),
            ("gate", "worker:w-1", MessageKind.LOCAL_FIX),
            # auditor never sends verifier's result kinds
            ("auditor", "planner", MessageKind.VERDICT),
        ],
    )
    def test_forbidden_routes(self, sender, receiver, kind):
        with pytest.raises(ValueError, match="route not allowed"):
            make(sender, receiver, kind)

    def test_human_sink_always_open(self):
        for sender in ("planner", "auditor", "verifier", "observer", "gate", "worker:w-1"):
            make(sender, "human", MessageKind.HUMAN)

    def test_human_only_receives_human_kind(self):
        with pytest.raises(ValueError):
            make("planner", "human", MessageKind.TASK_DISPATCH)


class TestIdempotency:
    def test_identical_message_reproduces_key(self):
        a = make("planner", "worker:w-1", MessageKind.TASK_DISPATCH, payload={"g": 1})
        b = make("planner", "worker:w-1", MessageKind.TASK_DISPATCH, payload={"g": 1})
        assert a.idempotency_key == b.idempotency_key

    def test_changed_payload_is_new_message(self):
        a = make("planner", "worker:w-1", MessageKind.TASK_DISPATCH, payload={"g": 1})
        b = make("planner", "worker:w-1", MessageKind.TASK_DISPATCH, payload={"g": 2})
        assert a.idempotency_key != b.idempotency_key

    def test_explicit_key_preserved(self):
        e = make("planner", "worker:w-1", MessageKind.LOCAL_FIX)
        e = Envelope(
            sender=e.sender, receiver=e.receiver, kind=e.kind,
            thread_id=e.thread_id, payload=e.payload,
            idempotency_key="fixed-key-123",
        )
        assert e.idempotency_key == "fixed-key-123"


class TestFingerprint:
    def test_normalization_dedups_formatting(self):
        a = issue_fingerprint("T1", "Pytest  FAILED\n x1", "worker:")
        b = issue_fingerprint("T1", "pytest failed x1", "worker:")
        assert a == b

    def test_namespaces_stay_distinct(self):
        a = issue_fingerprint("T1", "same error", "worker:")
        b = issue_fingerprint("T1", "same error", "observer:")
        assert a != b

    def test_blank_inputs_rejected(self):
        with pytest.raises(ValueError):
            issue_fingerprint(" ", "err", "worker:")


class TestSerialization:
    def test_json_roundtrip(self):
        e = make("planner", "worker:w-1", MessageKind.TASK_DISPATCH,
                 payload={"goal": "实现 divide", "paths": ["app.py"]})
        assert Envelope.from_json(e.to_json()) == e

    def test_worker_endpoint_validation(self):
        with pytest.raises(ValueError):
            Role.worker(" ")
        with pytest.raises(ValueError):
            make("worker:", "planner", MessageKind.BLOCKER_REPORT)
