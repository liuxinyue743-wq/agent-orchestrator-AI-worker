"""4.3 regression: bare file change is weak progress, test green is strong."""
from src.event_normalizer import EventNormalizer
from src.observer import Observer
from tests.util import CONFIG, ev


def test_file_change_is_weak_progress():
    n = EventNormalizer()
    act = {"id": "a1", "activityKind": "file_change", "status": "completed",
           "summary": "Modified app.py", "sequence": 1}
    e = n.from_activity("w1", "p1", act, {}, None)[0]
    assert e.progress is True
    assert e.progress_strength == "weak"


def test_test_improvement_is_strong_progress():
    n = EventNormalizer()
    e = n.make_strong_progress_event(
        project_id="p1", worker_id="w1", task_id=None,
        timestamp="2026-08-27T00:00:00Z", message="pytest passed")
    assert e.progress_strength == "strong"
    assert e.progress is True


def test_no_progress_ignores_weak_progress():
    """8 commands + 1 weak file change still fires NO_PROGRESS
    (weak progress does not cancel the alert)."""
    obs = Observer(CONFIG)
    alerts = []
    for i in range(8):
        alerts += obs.feed(ev("2026-08-27T00:%02d:00Z" % i,
                              etype="command_executed", message="c%d" % i))
    alerts += obs.feed(ev("2026-08-27T00:08:00Z", etype="file_changed",
                          progress=True, progress_strength="weak",
                          message="edited app.py"))
    assert len(alerts) == 1
    assert alerts[0].alert_type == "NO_PROGRESS"
    assert alerts[0].progress_count == 0


def test_no_progress_cancelled_by_strong():
    obs = Observer(CONFIG)
    alerts = []
    # A strong-progress event (test green) arrives inside the window before
    # activity hits the threshold -> NO_PROGRESS never fires.
    alerts += obs.feed(ev("2026-08-27T00:00:00Z", etype="test_result",
                          progress=True, progress_strength="strong",
                          message="pytest passed"))
    for i in range(8):
        alerts += obs.feed(ev("2026-08-27T00:%02d:00Z" % (i + 1),
                              etype="command_executed", message="c%d" % i))
    assert alerts == []
