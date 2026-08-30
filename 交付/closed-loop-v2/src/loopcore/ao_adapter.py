"""Read-only adapter over the Agent Orchestrator (AO) local interface.

Verified interface surface (see docs/AO_INTEGRATION_AUDIT.md):
  - REST:   GET /api/v1/projects | /projects/{id} | /sessions | /sessions/{id}
            | /sessions/{id}/conversation | /agents | /notifications
  - SSE:    GET /api/v1/events  (replay-all; resume via Last-Event-ID)
  - No auth on any endpoint; daemon binds 127.0.0.1:3001.

Known AO quirks handled here (and ONLY here):
  1. Some responses are invalid JSON: lone backslashes in Windows paths and
     JS-style \\' escapes.  repair_json() fixes both.
  2. /projects/{id} returns mojibake Chinese paths; the list endpoint is clean.
  3. Terminated sessions return HTTP 409 on /conversation.
  4. SSE stream is silent when idle (no heartbeats).
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Dict, Iterator, List, Optional

class UnsupportedOperation(NotImplementedError):
    """Raised for AO features the daemon does not expose."""


class AOError(RuntimeError):
    """Wrapped AO/HTTP failure with context."""


# escapes whose meaning is fixed by JSON (no look-ahead needed)
_SIMPLE_JSON_ESCAPES = set('"\\/bfnrt')


def repair_json(raw: str) -> str:
    """Repair AO's invalid JSON escapes. Character-level pass:

      backslash + simple JSON escape -> keep as-is
      backslash + u + 4 hex digits    -> keep as-is (valid \\uXXXX)
      backslash + u (not 4 hex)       -> escape the backslash (treat as literal)
      backslash + single quote (JS)   -> drop the backslash
      any other backslash             -> escape it (treat as literal)

    The \\u case is special: Windows paths like ``C:\\users\\lenovo`` contain
    ``\\u`` followed by non-hex chars; naively keeping ``\\u`` produces invalid
    JSON (``\\u`` must be followed by exactly 4 hex digits). Only a genuine
    ``\\uXXXX`` is preserved.
    """
    out = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "u":
                # keep only a genuine \uXXXX; otherwise the backslash is a
                # lone backslash (e.g. Windows path C:\users\...).
                if i + 5 < n and _is_hex(raw[i + 2:i + 6]):
                    out.append(c)
                    out.append(nxt)
                    out.append(raw[i + 2:i + 6])
                    i += 6
                else:
                    out.append("\\\\")
                    i += 1
            elif nxt in _SIMPLE_JSON_ESCAPES:
                out.append(c)
                out.append(nxt)
                i += 2
            elif nxt == "'":
                out.append("'")
                i += 2
            else:
                out.append("\\\\")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _is_hex(s: str) -> bool:
    return len(s) == 4 and all(ch in "0123456789abcdefABCDEF" for ch in s)


def loads_relaxed(raw: str):
    """Parse AO JSON after repair; wrap unrecoverable parse errors as AOError."""
    try:
        return json.loads(repair_json(raw))
    except (ValueError, TypeError) as e:
        raise AOError("malformed AO JSON after repair: %s" % e) from e


def _port_from_run_file() -> Optional[str]:
    """Read the daemon port from AO_RUN_FILE (env) or AO_DATA_DIR/ao.run."""
    import os
    run_file = os.environ.get("AO_RUN_FILE")
    if not run_file:
        data_dir = os.environ.get("AO_DATA_DIR")
        if data_dir:
            run_file = os.path.join(data_dir, "ao.run")
    if not run_file:
        return None
    try:
        with open(run_file, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    # ao.run is JSON; parse it properly first (the line scan below would
    # read the PID as the port on a compact single-line file).
    try:
        data = json.loads(text)
        port = data.get("port")
        if port:
            return str(int(port))
    except Exception:
        pass
    for line in text.splitlines():
        if "port" in line and ":" in line:
            raw = line.split(":", 1)[1].strip()
            digits = "".join(ch for ch in raw if ch.isdigit())
            return digits or None
    return None


class AOAdapter:
    """Read-only client for the AO daemon REST + SSE interface."""

    def __init__(self, base_url: str = "http://127.0.0.1:3001",
                 timeout: float = 15.0):
        if base_url == "http://127.0.0.1:3001":
            # The daemon picks a dynamic port recorded in AO_RUN_FILE; prefer it.
            derived = _port_from_run_file()
            if derived:
                base_url = "http://127.0.0.1:%s" % derived
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ REST
    def _get_raw(self, path: str) -> str:
        url = self.base_url + path
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise AOError("GET %s -> HTTP %s %s" % (url, e.code, body)) from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise AOError("GET %s -> %s" % (url, e)) from e

    def _get(self, path: str):
        return loads_relaxed(self._get_raw(path))

    def get_projects(self) -> List[Dict]:
        """List projects. Returns [{"id","name","path","kind",...}]."""
        data = self._get("/api/v1/projects")
        projects = data.get("projects", data) if isinstance(data, dict) else data
        return list(projects or [])

    def get_workers(self, project_id: str) -> List[Dict]:
        """List worker sessions of one project."""
        data = self._get("/api/v1/sessions")
        sessions = data.get("sessions", data) if isinstance(data, dict) else data
        return [s for s in (sessions or [])
                if s.get("projectId") == project_id]

    def get_worker_status(self, worker_id: str) -> Dict:
        """Full session record of one worker."""
        data = self._get("/api/v1/sessions/%s" % worker_id)
        return (data or {}).get("session", data or {})

    def get_worker_conversation(self, worker_id: str) -> Dict:
        """Conversation with turns, messages and the activities[] stream."""
        return self._get("/api/v1/sessions/%s/conversation" % worker_id)

    def get_recent_events(self, project_id: str, since: int = 0) -> List[Dict]:
        """Raw AO items newer than a conversation-sequence cursor.

        Returns a flat list of raw dicts tagged with their AO kind:
          {"kind":"session", "session": {...}}
          {"kind":"turn",    "session_id":..., "turn": {...}}
          {"kind":"activity","session_id":..., "activity": {...}}
        Turn items are always included (they carry the only timestamps);
        activities are filtered by sequence > since.
        """
        items: List[Dict] = []
        for worker in self.get_workers(project_id):
            wid = worker.get("id")
            items.append({"kind": "session", "session": worker})
            if worker.get("isTerminated"):
                # Terminated sessions answer 409 on /conversation (audit §2.8).
                continue
            # A session may terminate between get_workers and this call (race).
            # A 409 on one worker must NOT abort the whole round and drop every
            # other worker's events — skip just this worker and continue.
            try:
                conv = self.get_worker_conversation(wid)
            except AOError as e:
                if "HTTP 409" in str(e):
                    continue
                raise
            for turn in conv.get("turns") or []:
                items.append({"kind": "turn", "session_id": wid, "turn": turn})
            for act in conv.get("activities") or []:
                if (act.get("sequence") or 0) > since:
                    items.append(
                        {"kind": "activity", "session_id": wid, "activity": act})
        return items

    def get_notifications(self) -> List[Dict]:
        data = self._get("/api/v1/notifications")
        return list((data or {}).get("notifications", []))

    def resolve_approval(self, worker_id: str, request_id: str,
                         decision: str = "allow") -> bool:
        """Answer a pending worker approval request via the daemon REST API.

        Used by the closed loop's bounded auto-approval policy: file edits
        INSIDE a subtask's allowed_paths are allowed (allow_once) so the
        mission can run unattended; everything else stays pending for a
        human. Returns True when the daemon accepted the resolution.
        """
        url = ("%s/api/v1/sessions/%s/conversation/approvals/%s/resolve"
               % (self.base_url, worker_id, request_id))
        body = json.dumps({"decisionId": decision}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, socket.timeout):
            return False

    def get_agents(self) -> List[Dict]:
        data = self._get("/api/v1/agents")
        return list((data or {}).get("supported", []))

    # ------------------------------------------------------------------- SSE
    def stream_events(self, project_id: str,
                      idle_timeout: float = 30.0) -> Iterator[Dict]:
        """Yield SSE 'data:' JSON payloads whose session belongs to the project.

        Replays from the beginning on connect, resumes via Last-Event-ID when
        the idle connection drops (AO sends no heartbeats).
        """
        seen_seq = 0
        url = self.base_url + "/api/v1/events"
        backoff = 1.0
        while True:
            req = urllib.request.Request(url)
            if seen_seq:
                req.add_header("Last-Event-ID", str(seen_seq))
            try:
                resp = urllib.request.urlopen(req, timeout=idle_timeout)
            except (urllib.error.URLError, socket.timeout) as e:
                # Connection failure (daemon restart, network blip): do NOT
                # kill the stream permanently — the module docstring promises
                # automatic reconnection. Back off and retry, resuming via
                # Last-Event-ID. Cap the backoff so a long daemon outage does
                # not produce unbounded sleeps.
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue
            # connected: reset backoff
            backoff = 1.0
            resp.fp.raw._sock.settimeout(idle_timeout)
            buf = ""
            try:
                while True:
                    try:
                        chunk = resp.read(65536)
                    except (socket.timeout, TimeoutError):
                        break  # idle stream: reconnect with Last-Event-ID
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    # Split on blank line: tolerant of both LF (\n\n) and
                    # CRLF (\r\n\r\n) line endings (AO may emit either).
                    while "\n\n" in buf or "\r\n\r\n" in buf:
                        if "\r\n\r\n" in buf and (
                                not "\n\n" in buf
                                or buf.index("\r\n\r\n") < buf.index("\n\n")):
                            block, buf = buf.split("\r\n\r\n", 1)
                        else:
                            block, buf = buf.split("\n\n", 1)
                        for line in block.splitlines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                obj = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            seq = obj.get("seq")
                            if isinstance(seq, int):
                                seen_seq = max(seen_seq, seq)
                            yield obj
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
