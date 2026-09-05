"""Offline tests for the user directive channel (owner-ruled 2026-08-30).

Rules: the user may address ANY agent. Directives to the Planner stay
private; directives to any other endpoint are auto-mirrored to the Planner.
"""

from __future__ import annotations

import pytest

from loopcore.bus import LoopBus
from loopcore.envelope import Envelope, MessageKind


@pytest.fixture
def bus_setup():
    bus = LoopBus()
    boxes: dict[str, list[Envelope]] = {
        "planner": [], "auditor": [], "verifier": [], "worker:w-1": [],
    }
    for ep, box in boxes.items():
        bus.register(ep, box.append)
    return bus, boxes


def directive(receiver, text="改成用二分法", thread="U1"):
    return Envelope(sender="user", receiver=receiver,
                    kind=MessageKind.USER_DIRECTIVE, thread_id=thread,
                    payload={"directive": text})


class TestUserDirective:
    def test_user_can_address_every_role(self, bus_setup):
        bus, boxes = bus_setup
        bus.submit(directive("auditor", thread="U-a"))
        bus.submit(directive("verifier", thread="U-v"))
        bus.submit(directive("worker:w-1", thread="U-w"))
        assert len(boxes["auditor"]) == 1
        assert len(boxes["verifier"]) == 1
        assert len(boxes["worker:w-1"]) == 1
        # all three mirrored to the planner
        copies = [e for e in boxes["planner"]
                  if e.kind is MessageKind.USER_DIRECTIVE_COPY]
        assert len(copies) == 3
        assert {c.payload["originalReceiver"] for c in copies} == {
            "auditor", "verifier", "worker:w-1"}
        assert all(c.payload["directive"] == "改成用二分法" for c in copies)

    def test_planner_directive_stays_private(self, bus_setup):
        bus, boxes = bus_setup
        bus.submit(directive("planner", text="重新拆解 mission"))
        planner_msgs = boxes["planner"]
        assert len(planner_msgs) == 1
        assert planner_msgs[0].kind is MessageKind.USER_DIRECTIVE
        # no self-mirror copy
        assert all(e.kind is not MessageKind.USER_DIRECTIVE_COPY
                   for e in planner_msgs)

    def test_directive_to_user_itself_rejected(self):
        with pytest.raises(ValueError, match="route not allowed"):
            directive("user")

    def test_mirror_carries_original_msg_id(self, bus_setup):
        bus, boxes = bus_setup
        delivered = bus.submit(directive("auditor"))
        copy = boxes["planner"][0]
        assert copy.payload["originalMsgId"] == delivered.msg_id
        assert copy.thread_id == delivered.thread_id
