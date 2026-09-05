"""Sample A: same project+worker+fingerprint error >=3x in 10 min
-> REPEATED_ERROR.  Also covers negative cases (2 errors, spread-out errors).
"""
from loopcore.models import AlertType
from loopcore.event_observer import Observer
from tests.sidecar_port.util import CONFIG, ev

FINGERPRINT = "connection refused to <path>"


def feed_error(obs, ts, msg="connection refused to /tmp/x"):
    return obs.feed(ev(ts, etype="error", message=msg,
                      fingerprint=FINGERPRINT if msg.endswith("/tmp/x") else None))


def test_sample_a_repeated_error():
    obs = Observer(CONFIG)
    assert feed_error(obs, "2026-08-27T00:00:00Z") == []
    assert feed_error(obs, "2026-08-27T00:02:00Z") == []
    alerts = feed_error(obs, "2026-08-27T00:04:00Z")
    assert len(alerts) == 1
    al = alerts[0]
    assert al.alert_type == AlertType.REPEATED_ERROR.value
    assert al.project_id == "p1"
    assert al.worker_id == "w1"
    assert al.error_count == 3
    assert al.error_fingerprint == FINGERPRINT
    assert al.window_seconds == 600


def test_two_errors_no_alert():
    obs = Observer(CONFIG)
    feed_error(obs, "2026-08-27T00:00:00Z")
    assert feed_error(obs, "2026-08-27T00:01:00Z") == []


def test_spread_out_errors_no_alert():
    obs = Observer(CONFIG)
    feed_error(obs, "2026-08-27T00:00:00Z")
    feed_error(obs, "2026-08-27T00:03:00Z")
    # Third error 20 min later: only 1 error inside the 10-min window.
    assert feed_error(obs, "2026-08-27T00:20:00Z") == []


def test_different_fingerprints_no_alert():
    obs = Observer(CONFIG)
    feed_error(obs, "2026-08-27T00:00:00Z")
    feed_error(obs, "2026-08-27T00:01:00Z", msg="permission denied on /b")
    feed_error(obs, "2026-08-27T00:02:00Z", msg="timeout on /c")
    alerts = obs.feed(ev("2026-08-27T00:03:00Z", etype="command_executed"))
    assert alerts == []


def test_other_worker_not_merged():
    obs = Observer(CONFIG)
    feed_error(obs, "2026-08-27T00:00:00Z")                       # w1
    feed_error(obs, "2026-08-27T00:01:00Z")                       # w1
    obs.feed(ev("2026-08-27T00:02:00Z", worker="w2", etype="error",
                message="connection refused to /tmp/x",
                fingerprint=FINGERPRINT))
    # w1 has 2 errors, w2 has 1 -> no single worker reaches the threshold.
    assert obs.feed(ev("2026-08-27T00:03:00Z",
                       etype="command_executed")) == []
