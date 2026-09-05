"""Sample C: a normal worker with real progress must produce NO alerts,
even with interleaved activity and occasional (distinct) errors.

Real progress here means *strong* progress (tests turning green), not bare
file edits. A worker that only edits files without passing tests is NOT making
proven progress and would still trip NO_PROGRESS (see test_no_progress).
"""
from loopcore.event_observer import Observer
from tests.sidecar_port.util import CONFIG, ev


def minute(i, sec=0):
    return "2026-08-27T00:%02d:%02dZ" % (i, sec)


def test_sample_c_no_false_alert():
    obs = Observer(CONFIG)
    alerts = []
    # 10 commands, interleaved with 3 strong-progress events (tests green).
    for i in range(10):
        alerts += obs.feed(ev(minute(i), etype="command_executed",
                              message="command #%d" % i))
        if i in (2, 5, 8):
            alerts += obs.feed(ev(minute(i, sec=30), etype="test_result",
                                  progress=True,
                                  message="pytest passed #%d" % i))
    assert alerts == []


def test_occasional_distinct_errors_no_false_alert():
    obs = Observer(CONFIG)
    alerts = []
    msgs = ["connection refused", "permission denied", "timeout on /a"]
    for i, msg in enumerate(msgs):
        alerts += obs.feed(ev(minute(i), etype="error", message=msg,
                              fingerprint=msg))
    # One strong-progress event (test green) before activity accumulates, so
    # NO_PROGRESS can never fire (strong progress > 0 inside the window).
    alerts += obs.feed(ev(minute(3), etype="test_result", progress=True,
                          message="pytest passed"))
    for i in range(8):
        alerts += obs.feed(ev(minute(4 + i), etype="command_executed",
                              message="c%d" % i))
    assert alerts == []
