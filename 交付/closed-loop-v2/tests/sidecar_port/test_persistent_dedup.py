"""4.2 regression: process restart must not reprocess the same events/alerts.

The StateStore persists processed event_ids and fired alert_ids. A fresh
Observer backed by the same store skips items already recorded, even after the
in-memory deques are gone (simulating a restart).
"""
from loopcore.event_observer import Observer
from loopcore.state_store import StateStore
from tests.sidecar_port.util import CONFIG, ev

FP = "connection refused to /tmp/x"


def _err(ts, fp=FP, msg="connection refused to /tmp/x"):
    return ev(ts, etype="error", message=msg, fingerprint=fp)


def test_event_not_reprocessed_after_restart(tmp_path):
    store = StateStore(tmp_path / "cl.db")
    obs = Observer(CONFIG, state_store=store)
    # First run: 3 identical errors fire one REPEATED_ERROR.
    alerts = []
    for ts in ("2026-08-27T00:00:00Z", "2026-08-27T00:01:00Z",
               "2026-08-27T00:02:00Z"):
        alerts += obs.feed(_err(ts))
    assert len(alerts) == 1
    fired = alerts[0].alert_id

    # "Restart": new Observer, SAME store.
    store2 = StateStore(tmp_path / "cl.db")
    obs2 = Observer(CONFIG, state_store=store2)
    again = []
    for ts in ("2026-08-27T00:00:00Z", "2026-08-27T00:01:00Z",
               "2026-08-27T00:02:00Z"):
        again += obs2.feed(_err(ts))
    # No new alerts: same event_ids skipped, same alert_id already recorded.
    assert again == []
    store.close()
    store2.close()


def test_alert_fired_once_across_restart(tmp_path):
    store = StateStore(tmp_path / "cl.db")
    obs = Observer(CONFIG, state_store=store)
    # 8 activity events -> NO_PROGRESS
    alerts = []
    for i in range(8):
        alerts += obs.feed(ev("2026-08-27T00:%02d:00Z" % i,
                              etype="command_executed", message="c%d" % i))
    assert len(alerts) == 1
    first_id = alerts[0].alert_id

    store2 = StateStore(tmp_path / "cl.db")
    obs2 = Observer(CONFIG, state_store=store2)
    again = []
    for i in range(8):
        again += obs2.feed(ev("2026-08-27T00:%02d:00Z" % i,
                              etype="command_executed", message="c%d" % i))
    assert again == []
    store.close()
    store2.close()
