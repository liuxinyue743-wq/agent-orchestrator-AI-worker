"""Data models shared by the sidecar modules.

Every AO raw item is converted into a NormalizedEvent; the Observer consumes
NormalizedEvents and produces Alerts. Field names match schemas/event.schema.json
and schemas/alert.schema.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Source(str, Enum):
    AO_API = "ao_api"
    AO_SSE = "ao_sse"
    AO_WEBSOCKET = "ao_websocket"
    GIT = "git"
    TEST = "test"


class EventType(str, Enum):
    WORKER_STARTED = "worker_started"
    COMMAND_EXECUTED = "command_executed"
    ERROR = "error"
    FILE_CHANGED = "file_changed"
    TEST_RESULT = "test_result"
    COMMIT_CREATED = "commit_created"
    TASK_STATE_CHANGED = "task_state_changed"
    WORKER_FINISHED = "worker_finished"


class AlertType(str, Enum):
    REPEATED_ERROR = "REPEATED_ERROR"
    NO_PROGRESS = "NO_PROGRESS"


class ProgressStrength(str, Enum):
    """How strongly an event signals real task progress.

    none   : not progress (commands, errors, approvals, ...).
    weak   : file changed but task is not proven closer to done (edits, reverts,
             untested changes). NOT counted by NO_PROGRESS by default.
    strong : test turned green, valid commit, blocker lifted, gate passed.
    """
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"


@dataclass
class NormalizedEvent:
    event_id: str
    timestamp: str                      # UTC ISO-8601
    project_id: str
    task_id: Optional[str]
    worker_id: Optional[str]
    source: str
    event_type: str
    activity: bool
    progress: bool
    message: str
    fingerprint: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    progress_strength: str = "none"     # "none" | "weak" | "strong"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "source": self.source,
            "event_type": self.event_type,
            "activity": self.activity,
            "progress": self.progress,
            "progress_strength": self.progress_strength,
            "message": self.message,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
        }


@dataclass
class Alert:
    alert_id: str
    alert_type: str
    project_id: str
    worker_id: Optional[str]
    detected_at: str
    description: str
    window_seconds: Optional[int] = None
    error_fingerprint: Optional[str] = None
    error_count: Optional[int] = None
    activity_count: Optional[int] = None
    progress_count: Optional[int] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "project_id": self.project_id,
            "worker_id": self.worker_id,
            "detected_at": self.detected_at,
            "description": self.description,
            "window_seconds": self.window_seconds,
            "error_fingerprint": self.error_fingerprint,
            "error_count": self.error_count,
            "activity_count": self.activity_count,
            "progress_count": self.progress_count,
            "evidence": self.evidence,
        }
