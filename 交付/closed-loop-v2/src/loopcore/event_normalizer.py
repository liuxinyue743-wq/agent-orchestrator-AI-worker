"""Convert raw AO items into NormalizedEvents.

This is the ONLY module that knows AO field names. Business code (observer,
cli) only ever sees NormalizedEvent. Mapping rules (deterministic):

  AO session                  -> worker_started (first sight) /
                                 task_state_changed (activity.state change) /
                                 worker_finished (isTerminated)
  AO activity activityKind:
      "error"                 -> event_type "error", activity=True,  progress=False
      "command"               -> "command_executed", activity=True,  progress=False
      "mcp_tool"              -> "command_executed", activity=True,  progress=False
      "approval"              -> "task_state_changed", activity=True, progress=False
      "file_change" completed -> "file_changed", activity=True,    progress=True
      "file_change" failed    -> "error", activity=True, progress=False
      "reasoning"             -> dropped (thinking is not worker activity)
      anything else           -> "task_state_changed", activity=False, progress=False
  AO turn (completed, diff.files non-empty)
                              -> "file_changed", activity=True, progress=True
                                 (one event per turn, config-togglable)

Timestamps: AO activities carry no timestamp; best effort is turn requestedAt,
then worker lastActivityAt, then now. event_id is a deterministic sha1 of
(semantic kind, worker, id) so re-polling the same item never double-counts.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .fingerprints import Fingerprinter
from .models import NormalizedEvent

_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?\d*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse AO's ISO-8601 UTC strings (tolerates 7-digit fractions and Z)."""
    if not value:
        return None
    m = _ISO_TS.match(value.strip())
    if not m:
        return None
    base, frac, tz = m.group(1), (m.group(2) or ""), m.group(3)
    frac = (frac + "000000")[:6]
    s = base + ("." + frac if frac else "")
    if tz in (None, "", "Z"):
        s += "+00:00"
    elif len(tz) == 6 and tz[3] == ":":    # +08:00
        s += tz
    elif len(tz) == 5:                     # +0800
        s += tz[:3] + ":" + tz[3:]
    else:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(prefix: str) -> str:
    """Compact unique id for one-shot idempotency keys (audits, verifies,
    gate re-runs): '<prefix>-<YYYYMMDDTHHMMSSffffff>-<6 random hex>'.

    The random suffix kills same-microsecond collisions, and there are no
    punctuation fragments (the old `now_iso()[-12:]` slices kept '.' and
    '+0000' pieces, producing ids like VERIFY-.485976+0000 that were both
    ugly and collision-prone as dedup keys).
    """
    import uuid
    return "%s-%s-%s" % (
        prefix,
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
        uuid.uuid4().hex[:6])


def stable_id(prefix: str, *parts: str, length: int = 16) -> str:
    """Deterministic id derived FROM CONTENT (fingerprint, alert_id, audit
    id) where identical input MUST map to the identical id for dedup:
    '<prefix>-<sha1 hex[:length]>'. Replaces raw slicing of arbitrary
    strings, which could cut a JSON fingerprint mid-token and leak braces
    into the id (real: 'L0-{"code":-326')."""
    import hashlib
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8"))
    return "%s-%s" % (prefix, h.hexdigest()[:length])


def _epoch_seconds() -> int:
    """Whole-second UTC epoch time. Used by the runtime watchdog; kept here
    next to the other time helpers."""
    return int(datetime.now(timezone.utc).timestamp())


def _event_id(parts: List[str]) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]


class EventNormalizer:
    def __init__(self, fingerprint_options: Optional[Dict] = None,
                 turn_diff_counts_as_progress: bool = True):
        self.fp = Fingerprinter(fingerprint_options)
        self.turn_diff_as_progress = turn_diff_counts_as_progress
        self._worker_state: Dict[str, str] = {}   # worker_id -> last state
        self._worker_seen: set = set()            # worker ids already started
        self._finished_seen: set = set()

    # ------------------------------------------------------------ sessions
    def from_session(self, session: Dict) -> List[NormalizedEvent]:
        wid = session.get("id")
        if not wid:
            return []
        events: List[NormalizedEvent] = []
        state = str((session.get("activity") or {}).get("state", ""))
        last_at = (session.get("activity") or {}).get("lastActivityAt") \
            or session.get("updatedAt") or now_iso()
        project_id = session.get("projectId") or ""

        if wid not in self._worker_seen:
            self._worker_seen.add(wid)
            events.append(NormalizedEvent(
                event_id=_event_id(["session", wid, "started"]),
                timestamp=last_at, project_id=project_id, task_id=None,
                worker_id=wid, source="ao_api",
                event_type="worker_started", activity=False, progress=False,
                message="worker %s started" % wid,
                evidence={"harness": session.get("harness"),
                          "branch": session.get("branch")}))
        elif wid in self._worker_state and state and \
                self._worker_state[wid] != state:
            events.append(NormalizedEvent(
                event_id=_event_id(["session", wid, "state", state]),
                timestamp=last_at, project_id=project_id, task_id=None,
                worker_id=wid, source="ao_api",
                event_type="task_state_changed", activity=False, progress=False,
                message="worker %s state %s -> %s" % (
                    wid, self._worker_state[wid], state),
                evidence={"state": state}))
        self._worker_state[wid] = state

        if session.get("isTerminated") and wid not in self._finished_seen:
            self._finished_seen.add(wid)
            events.append(NormalizedEvent(
                event_id=_event_id(["session", wid, "finished"]),
                timestamp=last_at, project_id=project_id, task_id=None,
                worker_id=wid, source="ao_api",
                event_type="worker_finished", activity=False, progress=False,
                message="worker %s finished" % wid,
                evidence={"status": session.get("status")}))
        return events

    # ------------------------------------------------------------- turns
    def from_turn(self, session_id: str, project_id: str, turn: Dict) \
            -> List[NormalizedEvent]:
        if not self.turn_diff_as_progress:
            return []
        files = ((turn.get("diff") or {}).get("files")) or []
        if not files:
            return []
        ts = turn.get("completedAt") or turn.get("requestedAt") or now_iso()
        return [NormalizedEvent(
            event_id=_event_id(["turn", session_id, str(turn.get("id"))]),
            timestamp=ts, project_id=project_id, task_id=turn.get("id"),
            worker_id=session_id, source="ao_api",
            event_type="file_changed", activity=True, progress=True,
            progress_strength="weak",
            message="turn %s modified %d file(s)" % (
                str(turn.get("id"))[:8], len(files)),
            evidence={"files": files})]

    # --------------------------------------------------------- activities
    def from_activity(self, session_id: str, project_id: str, activity: Dict,
                      turn_times: Dict[str, str],
                      fallback_ts: Optional[str] = None) \
            -> List[NormalizedEvent]:
        kind = activity.get("activityKind") or activity.get("kind") or ""
        status = activity.get("status") or ""
        turn_id = activity.get("turnId")
        summary = activity.get("summary") or ""
        ts = (turn_times.get(turn_id) or fallback_ts or now_iso())

        if kind == "reasoning":
            return []  # thinking is not worker activity

        aid = activity.get("id") or ("%s-%s" % (kind, activity.get("sequence")))
        base = dict(event_id=_event_id(["activity", session_id, str(aid)]),
                    timestamp=ts, project_id=project_id, task_id=turn_id,
                    worker_id=session_id, source="ao_api",
                    evidence={"sequence": activity.get("sequence"),
                              "revision": activity.get("revision"),
                              "status": status,
                              "activityKind": kind})

        if kind == "error":
            fingerprint = self.fp.fingerprint(summary)
            return [NormalizedEvent(
                event_type="error", activity=True, progress=False,
                message=summary, fingerprint=fingerprint, **base)]
        if kind in ("command", "mcp_tool"):
            if status == "failed":
                # A failed command (e.g. failing test run) is an error.
                fingerprint = self.fp.fingerprint(summary)
                return [NormalizedEvent(
                    event_type="error", activity=True, progress=False,
                    message=summary, fingerprint=fingerprint, **base)]
            return [NormalizedEvent(
                event_type="command_executed", activity=True, progress=False,
                message=summary, **base)]
        if kind == "approval":
            return [NormalizedEvent(
                event_type="task_state_changed", activity=True, progress=False,
                message="approval: %s" % summary, **base)]
        if kind == "file_change":
            if status == "completed":
                return [NormalizedEvent(
                    event_type="file_changed", activity=True, progress=True,
                    progress_strength="weak",
                    message=summary, **base)]
            fingerprint = self.fp.fingerprint(summary)
            return [NormalizedEvent(
                event_type="error", activity=True, progress=False,
                message=summary, fingerprint=fingerprint, **base)]
        return [NormalizedEvent(
            event_type="task_state_changed", activity=False, progress=False,
            message="%s: %s" % (kind or "unknown", summary), **base)]

    # ---------------------------------------------------------------- SSE
    def from_sse_payload(self, payload: Dict, project_id: str,
                         worker_meta: Dict[str, Dict]) -> List[NormalizedEvent]:
        """Best-effort: SSE session updates carry sessionId + activity/state.

        Other payload shapes (notifications, bare ids) are ignored here; they
        carry no project-scoped worker event semantics.
        """
        sid = payload.get("sessionId")
        if not sid or sid not in worker_meta:
            return []
        session = dict(worker_meta[sid])
        if isinstance(payload.get("activity"), dict):
            session["activity"] = payload["activity"]
        session["isTerminated"] = bool(
            payload.get("isTerminated", session.get("isTerminated")))
        if "state" in payload and isinstance(payload.get("state"), str):
            session.setdefault("activity", {})["state"] = payload["state"]
        events = self.from_session(session)
        for ev in events:
            ev.source = "ao_sse"
        return events

    # ------------------------------------------------------------- helpers
    def make_error_event(self, *, project_id: str, worker_id: Optional[str],
                         task_id: Optional[str], timestamp: str,
                         message: str, source: str = "test") -> NormalizedEvent:
        """Convenience constructor used by tests/samples."""
        fingerprint = self.fp.fingerprint(message)
        return NormalizedEvent(
            event_id=_event_id(["synthetic", source, timestamp,
                                fingerprint, message]),
            timestamp=timestamp, project_id=project_id, task_id=task_id,
            worker_id=worker_id, source=source, event_type="error",
            activity=True, progress=False, progress_strength="none",
            message=message, fingerprint=fingerprint)

    def make_strong_progress_event(self, *, project_id: str,
                                   worker_id: Optional[str],
                                   task_id: Optional[str], timestamp: str,
                                   message: str, source: str = "test",
                                   event_type: str = "test_result") \
            -> NormalizedEvent:
        """A proven-progress event: test turned green, valid commit, gate pass.

        Such events have progress_strength='strong' and ARE counted by the
        NO_PROGRESS rule (unlike bare file changes, which are 'weak').
        """
        return NormalizedEvent(
            event_id=_event_id(["strong", source, timestamp, message]),
            timestamp=timestamp, project_id=project_id, task_id=task_id,
            worker_id=worker_id, source=source, event_type=event_type,
            activity=True, progress=True, progress_strength="strong",
            message=message, fingerprint=None,
            evidence={"strong": True})
