"""Sample B: >=8 activity events and 0 progress events in 15 min
-> NO_PROGRESS.  Also covers 7-activity negative and progress-rescue cases.
"""
from loopcore.models import AlertType
from loopcore.event_observer import Observer
from tests.sidecar_port.util import CONFIG, ev

T0 = "2026-08-27T00:00:00Z"


def minute(i):
    return "2026-08-27T00:%02d:00Z" % i


def test_sample_b_no_progress():
    obs = Observer(CONFIG)
    alerts = []
    for i in range(7):
        alerts += obs.feed(ev(minute(i), etype="command_executed",
                              message="command #%d" % i))
    assert alerts == []
    alerts += obs.feed(ev(minute(7), etype="command_executed",
                          message="command #7"))
    assert len(alerts) == 1
    al = alerts[0]
    assert al.alert_type == AlertType.NO_PROGRESS.value
    assert al.activity_count == 8
    assert al.progress_count == 0
    assert al.window_seconds == 900


def test_seven_activity_no_alert():
    obs = Observer(CONFIG)
    for i in range(7):
        assert obs.feed(ev(minute(i), etype="command_executed",
                           message="command #%d" % i)) == []


def test_progress_rescues_no_alert():
    obs = Observer(CONFIG)
    alerts = []
    for i in range(7):
        alerts += obs.feed(ev(minute(i), etype="command_executed",
                              message="command #%d" % i))
    # A strong-progress event (test turned green) cancels NO_PROGRESS.
    alerts += obs.feed(ev(minute(7), etype="test_result", progress=True,
                          progress_strength="strong",
                          message="pytest passed"))
    assert alerts == []


def test_weak_file_change_does_not_rescue():
    """A bare file edit is weak progress and must NOT cancel NO_PROGRESS."""
    obs = Observer(CONFIG)
    alerts = []
    for i in range(7):
        alerts += obs.feed(ev(minute(i), etype="command_executed",
                              message="command #%d" % i))
    # 8th activity is a file change (weak progress): activity hits threshold,
    # progress stays 0 strong -> NO_PROGRESS fires.
    alerts += obs.feed(ev(minute(7), etype="file_changed", progress=True,
                          progress_strength="weak",
                          message="modified app.py"))
    assert len(alerts) == 1
    assert alerts[0].alert_type == "NO_PROGRESS"


def test_activity_spread_beyond_window_no_alert():
    obs = Observer(CONFIG)
    alerts = []
    for i in range(8):
        # 1 hour apart: only 1 event lands inside any 15-min window.
        alerts += obs.feed(ev("2026-08-27T%02d:00:00Z" % i,
                              etype="command_executed", message="c%d" % i))
    assert alerts == []
