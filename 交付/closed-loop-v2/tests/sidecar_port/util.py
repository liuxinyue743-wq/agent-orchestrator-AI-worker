"""Shared builders for synthetic NormalizedEvents (samples A/B/C)."""
import hashlib

from loopcore.models import NormalizedEvent

# Thresholds mirror config/default.yaml so tests pin the spec values.
CONFIG = {
    "thresholds": {
        "repeated_error": {"window_seconds": 600, "count": 3,
                           "cooldown_seconds": 600},
        "no_progress": {"window_seconds": 900, "min_activity_events": 8,
                        "max_progress_events": 0, "cooldown_seconds": 900},
    }
}


def ev(ts, project="p1", worker="w1", etype="command_executed",
       activity=True, progress=False, message="cmd", fingerprint=None,
       event_id=None, progress_strength=None):
    if event_id is None:
        event_id = hashlib.sha1(
            (ts + etype + message + str(fingerprint)).encode()).hexdigest()[:24]
    if progress_strength is None:
        progress_strength = "strong" if progress and etype != "file_changed" \
            else ("weak" if progress else "none")
    return NormalizedEvent(
        event_id=event_id, timestamp=ts, project_id=project, task_id=None,
        worker_id=worker, source="test", event_type=etype, activity=activity,
        progress=progress, progress_strength=progress_strength,
        message=message, fingerprint=fingerprint)
