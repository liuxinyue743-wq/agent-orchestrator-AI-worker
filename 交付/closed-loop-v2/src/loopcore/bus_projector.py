"""Project StateStore rows onto the Loop Bus and the project memory files.

The proven mission/closed-loop controllers record every alert, audit, planner
action, gate run, verifier verdict and state transition in the StateStore.
This projector tails those tables and re-emits each NEW row as a route-checked
Envelope on the Loop Bus, and appends human-readable entries to
memory.md / project.md.

The control path stays in the proven controllers. The Bus is the audited
communication trail required by ARCHITECTURE-v0.2.md: every projected envelope
must pass the same direction matrix, idempotency and hop budgets as any live
message — so a real mission exercises the routing rules end to end.

Projection is an audit trail, never a control path: a rejected envelope lands
in ``errors`` and never crashes the mission.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bus import BusError, LoopBus
from .envelope import Envelope, MessageKind
from .memory import MemoryEntry, ProjectMemory
from .state_store import StateStore

# planner_actions.action -> (bus kind, receiver role). CONTINUE and unknown
# actions are intentionally not projected (they produce no message traffic).
_ACTION_ROUTES = {
    "SEND_LOCAL_FIX": (MessageKind.LOCAL_FIX, "worker"),
    "REPLAN_SPAWN": (MessageKind.REPLAN_DISPATCH, "worker"),
    "CANDIDATE_DONE": (MessageKind.GATE_RUN, "gate"),
    "HUMAN": (MessageKind.HUMAN, "human"),
}

_TABLES = ("alerts", "audits", "planner_actions", "verifications",
           "gate_runs", "state_transitions", "missions")


class StoreBusProjector:
    """Tail one StateStore and project new rows onto the Bus and memory files."""

    def __init__(
        self,
        store: StateStore,
        bus: LoopBus,
        memory: Optional[ProjectMemory] = None,
        *,
        traffic_log: Optional[str | Path] = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.memory = memory
        self.errors: List[str] = []
        self.projected: List[Envelope] = []
        self._traffic_log = Path(traffic_log) if traffic_log else None
        if self._traffic_log:
            self._traffic_log.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- cursors
    def _cursor(self, table: str) -> int:
        return int(self.store.counter_get("busproj:" + table))

    def _advance(self, table: str, rowid: int) -> None:
        self.store.counter_set("busproj:" + table, rowid)

    # ------------------------------------------------------------------ main
    def project_once(self) -> int:
        """Project every row newer than the per-table high-water mark.

        Returns the number of envelopes submitted to the Bus this pass.
        """
        submitted = 0
        with self.store._lock:
            rows_by_table: Dict[str, List[tuple]] = {}
            for table in _TABLES:
                cur = self.store._conn.execute(
                    f"SELECT rowid, * FROM {table} WHERE rowid > ?"
                    " ORDER BY rowid",
                    (self._cursor(table),),
                )
                rows_by_table[table] = cur.fetchall()
        for table, rows in rows_by_table.items():
            for row in rows:
                before = len(self.projected)
                self._project_row(table, row)
                self._advance(table, row[0])
                submitted += len(self.projected) - before
        return submitted

    # ------------------------------------------------------------------ rows
    def _project_row(self, table: str, row: tuple) -> None:
        try:
            if table == "alerts":
                payload = json.loads(row[2])
                self._emit(
                    "observer", "auditor", MessageKind.TRIGGER,
                    thread_id="alert-" + str(row[1]),
                    payload={"alert": payload},
                )
            elif table == "audits":
                task_id, payload = row[2], json.loads(row[3])
                self._emit(
                    "auditor", "planner", MessageKind.AUDIT_REPORT,
                    thread_id=str(task_id or row[1]),
                    payload={"audit": payload},
                )
            elif table == "planner_actions":
                task_id, payload = row[2], json.loads(row[3])
                self._project_action(str(task_id or row[1]), payload)
            elif table == "verifications":
                task_id, payload = row[2], json.loads(row[3])
                self._emit(
                    "verifier", "planner", MessageKind.VERDICT,
                    thread_id=str(task_id or row[1]),
                    payload={"verdict": payload},
                )
            elif table == "gate_runs":
                # (rowid, id, task_id, command, cwd, exit_code, started, ...)
                # run rowid distinguishes legitimate repeats of the same
                # command on the same task (strict-equal-body idempotency
                # would otherwise collapse them into false duplicates).
                self._emit(
                    "gate", "planner", MessageKind.GATE_EVIDENCE,
                    thread_id=str(row[2] or ("gate-%d" % row[0])),
                    payload={"command": row[3], "exit_code": row[5],
                             "ok": row[5] == 0, "run": row[0]},
                )
            elif table == "state_transitions":
                self._project_transition(row)
            elif table == "missions":
                self._project_mission(row)
        except (ValueError, BusError) as exc:
            self.errors.append(f"{table} rowid={row[0]}: {exc}")
        except (json.JSONDecodeError, TypeError) as exc:
            self.errors.append(f"{table} rowid={row[0]}: bad payload: {exc}")

    def _project_action(self, thread_id: str, payload: Dict[str, Any]) -> None:
        route = _ACTION_ROUTES.get(str(payload.get("action")))
        if route is None:
            return
        kind, receiver_role = route
        if receiver_role == "worker":
            target = payload.get("target_session_id")
            if not target:
                return  # no concrete worker endpoint; nothing to project
            receiver = f"worker:{target}"
        else:
            receiver = receiver_role
        self._emit("planner", receiver, kind, thread_id=thread_id,
                   payload={"action": payload})

    def _project_transition(self, row: tuple) -> None:
        # (rowid, id, task_id, from_state, to_state, actor, reason,
        #  evidence_json, timestamp)
        _, _, task_id, frm, to, actor, reason, evidence_json, ts = row[:9]
        if self.memory is not None and task_id:
            self.memory.append(MemoryEntry(
                target="memory",
                heading=f"{task_id}: {frm} → {to}",
                body=f"actor={actor}\n\nreason={reason}",
            ))
            if to in ("DONE", "HUMAN", "FAILED"):
                self.memory.append(MemoryEntry(
                    target="project",
                    heading=f"{task_id} 到达 {to}",
                    body=f"actor={actor}\n\nreason={reason}",
                ))

    def _project_mission(self, row: tuple) -> None:
        # (rowid, mission_id, payload_json, recorded_at); terminal states emit
        # the Planner's FINAL_REPORT to the user (the single result channel).
        mission_id = str(row[1])
        payload = json.loads(row[2])
        state = str(payload.get("state") or "")
        reason = str(payload.get("reason") or "")
        if self.memory is not None:
            self.memory.append(MemoryEntry(
                target="project",
                heading=f"mission {mission_id}: {state or '更新'}",
                body=reason or "state recorded",
            ))
        if state in ("MISSION_DONE", "HUMAN", "FAILED"):
            self._emit("planner", "user", MessageKind.FINAL_REPORT,
                       thread_id=mission_id,
                       payload={"state": state, "reason": reason})

    # ------------------------------------------------------------------ emit
    def _emit(self, sender: str, receiver: str, kind: MessageKind, *,
              thread_id: str, payload: Dict[str, Any]) -> None:
        envelope = Envelope(sender=sender, receiver=receiver, kind=kind,
                            thread_id=thread_id, payload=payload)
        self._ensure_sink(receiver)
        delivered = self.bus.submit(envelope)
        self.projected.append(delivered)
        if self._traffic_log:
            with open(self._traffic_log, "a", encoding="utf-8") as fh:
                fh.write(delivered.to_json() + "\n")

    def _ensure_sink(self, endpoint: str) -> None:
        """Register a no-op sink for endpoints with no live handler yet.

        In the projection phase the Bus is the trail, not the transport; a
        sink simply accepts delivery so routing/budget rules still execute.
        """
        if self.bus.has_endpoint(endpoint):
            return
        self.bus.register(endpoint, lambda env: None)
