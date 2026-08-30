"""Persistent state store for the closed-loop sidecar.

SQLite-backed (stdlib only). Provides idempotency across process restarts:

  - same AO event_id is never processed twice
  - same alert_id is never fired twice
  - same AuditResult never sent to Planner twice
  - same PlannerAction never executed twice

Also records project state-machine transitions, gate runs, tasks.

Tables are created on first connect. Tests pass a temp DB path and may wipe it.
This store is the ONLY persistence layer; it must not depend on AO's internal DB.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  from_state TEXT,
  to_state TEXT,
  actor TEXT,
  reason TEXT,
  evidence_json TEXT,
  timestamp TEXT
);
CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS audits (
  audit_id TEXT PRIMARY KEY,
  task_id TEXT,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
  verify_id TEXT PRIMARY KEY,
  task_id TEXT,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS missions (
  mission_id TEXT PRIMARY KEY,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS planner_actions (
  action_id TEXT PRIMARY KEY,
  task_id TEXT,
  payload_json TEXT,
  recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS executed_actions (
  action_id TEXT PRIMARY KEY,
  executed_at TEXT,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS gate_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  command TEXT,
  cwd TEXT,
  exit_code INTEGER,
  started_at TEXT,
  ended_at TEXT,
  stdout TEXT,
  stderr TEXT
);
CREATE TABLE IF NOT EXISTS counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0
);
"""


class StateStore:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: the store is shared by poll + SSE threads.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ tasks
    def record_task(self, task_id: str, spec_json: Dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks(task_id,spec_json) VALUES(?,?)",
                (task_id, json.dumps(spec_json, ensure_ascii=False)))
            self._conn.commit()

    def load_task(self, task_id: str) -> Optional[Dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT spec_json FROM tasks WHERE task_id=?", (task_id,))
            r = cur.fetchone()
        return json.loads(r[0]) if r else None

    def all_task_ids(self) -> list:
        with self._lock:
            cur = self._conn.execute("SELECT task_id FROM tasks")
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------ events
    def event_seen(self, event_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id=?", (event_id,))
            return cur.fetchone() is not None

    def record_event(self, event_id: str, payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_events(event_id,payload_json,recorded_at)"
                " VALUES(?,?,?)",
                (event_id, json.dumps(payload, ensure_ascii=False), now_iso()))
            self._conn.commit()

    # ------------------------------------------------------------ alerts
    def alert_seen(self, alert_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM alerts WHERE alert_id=?", (alert_id,))
            return cur.fetchone() is not None

    def record_alert(self, alert_id: str, payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO alerts(alert_id,payload_json,recorded_at)"
                " VALUES(?,?,?)",
                (alert_id, json.dumps(payload, ensure_ascii=False), now_iso()))
            self._conn.commit()

    # ------------------------------------------------------------ audits
    def audit_seen(self, audit_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM audits WHERE audit_id=?", (audit_id,))
            return cur.fetchone() is not None

    def record_audit(self, audit_id: str, task_id: str, payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO audits(audit_id,task_id,payload_json,recorded_at)"
                " VALUES(?,?,?,?)",
                (audit_id, task_id, json.dumps(payload, ensure_ascii=False),
                 now_iso()))
            self._conn.commit()

    # ------------------------------------------------------- verifications
    def verification_seen(self, verify_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM verifications WHERE verify_id=?", (verify_id,))
            return cur.fetchone() is not None

    def record_verification(self, verify_id: str, task_id: str,
                            payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO verifications(verify_id,task_id,payload_json,recorded_at)"
                " VALUES(?,?,?,?)",
                (verify_id, task_id, json.dumps(payload, ensure_ascii=False),
                 now_iso()))
            self._conn.commit()

    # ---------------------------------------------------------- missions
    def record_mission(self, mission_id: str, payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO missions(mission_id,payload_json,recorded_at)"
                " VALUES(?,?,?)",
                (mission_id, json.dumps(payload, ensure_ascii=False),
                 now_iso()))
            self._conn.commit()

    # ------------------------------------------------------- planner actions
    def action_seen(self, action_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM planner_actions WHERE action_id=?", (action_id,))
            return cur.fetchone() is not None

    def record_action(self, action_id: str, task_id: str, payload: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO planner_actions(action_id,task_id,payload_json,recorded_at)"
                " VALUES(?,?,?,?)",
                (action_id, task_id, json.dumps(payload, ensure_ascii=False),
                 now_iso()))
            self._conn.commit()

    def action_executed(self, action_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM executed_actions WHERE action_id=?", (action_id,))
            return cur.fetchone() is not None

    def mark_action_executed(self, action_id: str, result: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO executed_actions(action_id,executed_at,result_json)"
                " VALUES(?,?,?)",
                (action_id, now_iso(), json.dumps(result, ensure_ascii=False)))
            self._conn.commit()

    # ------------------------------------------------------------ transitions
    def record_transition(self, *, task_id: str, from_state: str,
                          to_state: str, actor: str, reason: str,
                          evidence: Dict) -> None:
        from .event_normalizer import now_iso
        with self._lock:
            self._conn.execute(
                "INSERT INTO state_transitions(task_id,from_state,to_state,actor,reason,evidence_json,timestamp)"
                " VALUES(?,?,?,?,?,?,?)",
                (task_id, from_state, to_state, actor, reason,
                 json.dumps(evidence, ensure_ascii=False), now_iso()))
            self._conn.commit()

    def latest_state(self, task_id: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT to_state FROM state_transitions WHERE task_id=? "
                "ORDER BY id DESC LIMIT 1", (task_id,))
            row = cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------ gate
    def record_gate_run(self, *, task_id: str, command: str, cwd: str,
                       exit_code: int, started_at: str, ended_at: str,
                       stdout: str, stderr: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO gate_runs(task_id,command,cwd,exit_code,started_at,ended_at,stdout,stderr)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (task_id, command, cwd, exit_code, started_at, ended_at,
                 stdout, stderr))
            self._conn.commit()

    # ------------------------------------------------------------ counters
    def counter_get(self, name: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM counters WHERE name=?", (name,))
            row = cur.fetchone()
            return row[0] if row else 0

    def counter_incr(self, name: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO counters(name,value) VALUES(?,1) "
                "ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT value FROM counters WHERE name=?", (name,))
            return cur.fetchone()[0]

    def counter_set(self, name: str, value: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO counters(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, int(value)))
            self._conn.commit()
