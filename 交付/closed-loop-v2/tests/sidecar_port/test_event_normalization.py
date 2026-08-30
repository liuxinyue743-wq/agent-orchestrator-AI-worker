"""Normalizer mapping: AO raw items -> NormalizedEvent."""
from loopcore.event_normalizer import EventNormalizer, parse_ts
from loopcore.models import EventType


def test_parse_ts_variants():
    assert parse_ts("2026-08-27T05:22:18Z") is not None
    assert parse_ts("2026-08-27T05:22:05.8404262Z") is not None  # 7-digit frac
    assert parse_ts("2026-08-27T05:22:05.227036Z") is not None
    assert parse_ts("not-a-time") is None
    assert parse_ts(None) is None


def test_session_lifecycle():
    n = EventNormalizer()
    sess = {"id": "w1", "projectId": "p1",
            "activity": {"state": "idle",
                         "lastActivityAt": "2026-08-27T05:22:18Z"},
            "harness": "codex"}
    first = n.from_session(sess)
    assert [e.event_type for e in first] == [EventType.WORKER_STARTED.value]
    assert first[0].worker_id == "w1"
    # Same state again -> no new events (dedup by id).
    assert n.from_session(sess) == []
    # State change -> task_state_changed.
    sess["activity"] = {"state": "running",
                        "lastActivityAt": "2026-08-27T05:23:00Z"}
    second = n.from_session(sess)
    assert [e.event_type for e in second] == [
        EventType.TASK_STATE_CHANGED.value]
    # Terminated -> worker_finished.
    sess["activity"] = {"state": "exited",
                        "lastActivityAt": "2026-08-27T05:30:00Z"}
    sess["isTerminated"] = True
    third = n.from_session(sess)
    types = [e.event_type for e in third]
    assert EventType.WORKER_FINISHED.value in types


def test_error_activity_maps_to_error_with_fingerprint():
    n = EventNormalizer()
    act = {"id": "a1", "activityKind": "error", "status": "failed",
           "summary": "provider error: connection refused to /tmp/x",
           "turnId": "t1", "sequence": 2}
    events = n.from_activity("w1", "p1", act,
                             {"t1": "2026-08-27T05:22:18Z"})
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EventType.ERROR.value
    assert ev.activity and not ev.progress
    assert ev.fingerprint
    assert ev.timestamp == "2026-08-27T05:22:18Z"
    assert ev.task_id == "t1"


def test_file_change_completed_is_progress():
    n = EventNormalizer()
    act = {"id": "a2", "activityKind": "file_change", "status": "completed",
           "summary": "Modified app.py", "sequence": 3}
    events = n.from_activity("w1", "p1", act, {}, None)
    assert events[0].event_type == EventType.FILE_CHANGED.value
    assert events[0].progress


def test_failed_command_is_error():
    n = EventNormalizer()
    act = {"id": "a9", "activityKind": "command", "status": "failed",
           "summary": 'powershell.exe -Command "python -m pytest tests -q"',
           "sequence": 6}
    events = n.from_activity("w1", "p1", act, {}, None)
    assert events[0].event_type == EventType.ERROR.value
    assert events[0].activity and not events[0].progress
    assert events[0].fingerprint


def test_completed_command_is_command_executed():
    n = EventNormalizer()
    act = {"id": "a10", "activityKind": "command", "status": "completed",
           "summary": "Get-Location", "sequence": 7}
    events = n.from_activity("w1", "p1", act, {}, None)
    assert events[0].event_type == EventType.COMMAND_EXECUTED.value


def test_reasoning_dropped():
    n = EventNormalizer()
    act = {"id": "a3", "activityKind": "reasoning", "status": "completed",
           "summary": "Reasoning", "sequence": 4}
    assert n.from_activity("w1", "p1", act, {}, None) == []


def test_turn_with_diff_is_progress():
    n = EventNormalizer()
    turn = {"id": "t9", "state": "completed",
            "requestedAt": "2026-08-27T05:22:18Z",
            "completedAt": "2026-08-27T05:24:37Z",
            "diff": {"files": [{"path": "app.py", "additions": 6,
                                "deletions": 2, "status": "modified"}]}}
    events = n.from_turn("w1", "p1", turn)
    assert len(events) == 1
    assert events[0].event_type == EventType.FILE_CHANGED.value
    assert events[0].progress
    # Turn without diff -> no progress event.
    assert n.from_turn("w1", "p1", {"id": "t10", "diff": {}}) == []


def test_event_id_stable_across_calls():
    n = EventNormalizer()
    act = {"id": "a5", "activityKind": "error", "status": "failed",
           "summary": "boom", "sequence": 9}
    e1 = n.from_activity("w1", "p1", act, {}, "2026-08-27T05:22:18Z")[0]
    e2 = n.from_activity("w1", "p1", act, {}, "2026-08-27T05:22:18Z")[0]
    assert e1.event_id == e2.event_id
