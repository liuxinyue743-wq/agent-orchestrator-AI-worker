"""Deterministic supervision rules over NormalizedEvents.

Two rules, thresholds read from config (never hardcoded):

  REPEATED_ERROR  same (project, worker, fingerprint) error event
                  >= count times within window_seconds
  NO_PROGRESS     >= min_activity_events activity events and
                  <= max_progress_events progress events within window_seconds

NO_PROGRESS counts only *strong* progress by default (test green, commit, gate
pass). Bare file edits are 'weak' progress and do NOT cancel the alert — they
may represent reverts or thrash. Weak/strong is decided in event_normalizer.

Concurrency: every public mutator takes an RLock, so polling and SSE threads
can feed the same Observer safely (4.4).
"""
from __future__ import annotations

import hashlib
import threading
from collections import deque
from datetime import timedelta
from typing import Deque, Dict, List, Optional, Tuple

from .event_normalizer import now_iso, parse_ts
from .models import Alert, AlertType, NormalizedEvent

EventKey = Tuple[str, str]          # (project_id, worker_id or "")
AlertKey = Tuple[str, str, str, str]


def _alert_id(key: AlertKey, detected_at: str) -> str:
    raw = "|".join(key) + "|" + detected_at
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


class Observer:
    def __init__(self, config: Dict, state_store=None):
        self.re_err = config["thresholds"]["repeated_error"]
        self.no_prog = config["thresholds"]["no_progress"]
        # NO_PROGRESS counts only strong progress unless explicitly widened.
        np_cfg = self.no_prog
        self.progress_mode = np_cfg.get("progress_mode", "strong")
        self._events: Dict[EventKey, Deque[Dict]] = {}
        self._seen: Dict[EventKey, set] = {}
        self._last_alert: Dict[AlertKey, str] = {}
        self._max_window = max(self.re_err.get("window_seconds", 600),
                               self.no_prog.get("window_seconds", 900))
        self._max_deque = 10000
        self._lock = threading.RLock()
        # Optional persistent dedup backend (src.state_store.StateStore).
        # When set, event_ids and alert_ids are also checked/recorded there so a
        # process restart never reprocesses the same AO history (4.2).
        self._store = state_store

    # ------------------------------------------------------------------ feed
    def feed(self, ev: NormalizedEvent) -> List[Alert]:
        with self._lock:
            key = (ev.project_id, ev.worker_id or "")
            seen = self._seen.setdefault(key, set())
            if ev.event_id in seen:
                return []
            # Persistent dedup: a restarted process skips events already handled.
            if self._store is not None and self._store.event_seen(ev.event_id):
                seen.add(ev.event_id)
                return []
            seen.add(ev.event_id)
            if self._store is not None:
                self._store.record_event(ev.event_id, ev.to_dict())
            dq = self._events.setdefault(key, deque())
            dq.append({
                "ts_str": ev.timestamp,
                "ts": parse_ts(ev.timestamp),
                "event_type": ev.event_type,
                "activity": ev.activity,
                "progress": ev.progress,
                "progress_strength": ev.progress_strength,
                "fingerprint": ev.fingerprint,
                "message": ev.message,
                "event_id": ev.event_id,
            })
            self._trim(key, dq)
            return self._evaluate(key, ev, dq)

    def _trim(self, key: EventKey, dq: Deque[Dict]) -> None:
        while len(dq) > self._max_deque:
            old = dq.popleft()
            self._seen[key].discard(old["event_id"])

    def _window(self, dq: Deque[Dict], seconds: int) -> List[Dict]:
        valid = [e for e in dq if e["ts"] is not None]
        if not valid:
            return []
        ref = valid[-1]["ts"]
        cutoff = ref - timedelta(seconds=seconds)
        return [e for e in valid if e["ts"] >= cutoff]

    # ----------------------------------------------------------------- rules
    def _evaluate(self, key: EventKey, ev: NormalizedEvent,
                  dq: Deque[Dict]) -> List[Alert]:
        alerts: List[Alert] = []
        alerts += self._check_repeated_error(key, dq)
        alerts += self._check_no_progress(key, dq)
        return alerts

    def _check_repeated_error(self, key: EventKey,
                              dq: Deque[Dict]) -> List[Alert]:
        window = self._window(dq, self.re_err["window_seconds"])
        by_fp: Dict[str, List[Dict]] = {}
        for e in window:
            if e["event_type"] == "error":
                by_fp.setdefault(e["fingerprint"] or "(no-fingerprint)", []).append(e)
        alerts = []
        for fp, group in by_fp.items():
            if len(group) < self.re_err["count"]:
                continue
            akey = (key[0], key[1], AlertType.REPEATED_ERROR.value, fp)
            if not self._cooldown_ok(akey, self.re_err["cooldown_seconds"]):
                continue
            detected = now_iso()
            alert_id = _alert_id(akey, detected)
            if self._store is not None and self._store.alert_seen(alert_id):
                self._last_alert[akey] = detected
                continue
            self._last_alert[akey] = detected
            alerts.append(Alert(
                alert_id=alert_id,
                alert_type=AlertType.REPEATED_ERROR.value,
                project_id=key[0], worker_id=key[1] or None,
                detected_at=detected,
                description=("worker %s repeated the same error %d time(s) "
                             "within %d s (fingerprint: %s)"
                             % (key[1], len(group),
                                self.re_err["window_seconds"], fp)),
                window_seconds=self.re_err["window_seconds"],
                error_fingerprint=fp, error_count=len(group),
                evidence={"sample_messages": [e["message"] for e in group[-3:]],
                          "event_ids": [e["event_id"] for e in group[-3:]]}))
            if self._store is not None:
                self._store.record_alert(alert_id, alerts[-1].to_dict())
        return alerts

    def _check_no_progress(self, key: EventKey, dq: Deque[Dict]) -> List[Alert]:
        window = self._window(dq, self.no_prog["window_seconds"])
        activity = sum(1 for e in window if e["activity"])
        if self.progress_mode == "strong":
            progress = sum(1 for e in window
                            if e.get("progress_strength") == "strong")
        else:
            progress = sum(1 for e in window if e["progress"])
        if activity < self.no_prog["min_activity_events"]:
            return []
        if progress > self.no_prog["max_progress_events"]:
            return []
        akey = (key[0], key[1], AlertType.NO_PROGRESS.value, "")
        if not self._cooldown_ok(akey, self.no_prog["cooldown_seconds"]):
            return []
        detected = now_iso()
        alert_id = _alert_id(akey, detected)
        if self._store is not None and self._store.alert_seen(alert_id):
            self._last_alert[akey] = detected
            return []
        self._last_alert[akey] = detected
        alert = Alert(
            alert_id=alert_id,
            alert_type=AlertType.NO_PROGRESS.value,
            project_id=key[0], worker_id=key[1] or None,
            detected_at=detected,
            description=("worker %s has %d activity event(s) but %d progress "
                         "event(s) within %d s"
                         % (key[1], activity, progress,
                            self.no_prog["window_seconds"])),
            window_seconds=self.no_prog["window_seconds"],
            activity_count=activity, progress_count=progress,
            evidence={"sample_messages": [e["message"] for e in window[-3:]],
                      "progress_mode": self.progress_mode})
        if self._store is not None:
            self._store.record_alert(alert_id, alert.to_dict())
        return [alert]

    def _cooldown_ok(self, akey: AlertKey, seconds: int) -> bool:
        last = self._last_alert.get(akey)
        if not last:
            return True
        ts = parse_ts(last)
        if ts is None:
            return True
        now = parse_ts(now_iso())
        if now is None:
            return True
        return (now - ts).total_seconds() >= seconds
