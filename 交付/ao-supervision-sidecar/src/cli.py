"""AO supervision sidecar entry point.

  python -m src.cli --once [--project ID]   one snapshot: fetch, normalize,
                                            run rules, write runtime files
  python -m src.cli --watch [--use-sse]     loop; optional live SSE feed

Runtime files (append mode): runtime/events.jsonl, runtime/alerts.jsonl.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .ao_adapter import AOAdapter, AOError
from .event_normalizer import EventNormalizer
from .models import Alert, NormalizedEvent
from .observer import Observer
from .state_store import StateStore

ROOT = Path(__file__).resolve().parent.parent

# Process-wide lock so the SSE thread and the poll loop never interleave
# half-written JSONL lines (4.4).
_JSONL_LOCK = threading.Lock()


def load_config(path: Optional[str]) -> Dict:
    cfg_path = Path(path) if path else ROOT / "config" / "default.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def _append_jsonl(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _JSONL_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


class Snapshot:
    """One full poll cycle: fetch -> normalize -> observe -> persist."""

    def __init__(self, cfg: Dict, fresh: bool = False,
                 db_path: Optional[str] = None):
        self.cfg = cfg
        self.adapter = AOAdapter(
            cfg["ao"]["base_url"],
            timeout=float(cfg["ao"].get("request_timeout_seconds", 15)))
        self.normalizer = EventNormalizer(
            cfg.get("fingerprint"),
            bool(cfg.get("observer", {}).get("turn_diff_counts_as_progress",
                                             True)))
        self.events_file = _resolve(ROOT, cfg["runtime"]["events_file"])
        self.alerts_file = _resolve(ROOT, cfg["runtime"]["alerts_file"])
        db = db_path or str(ROOT / "runtime" / "closed_loop.db")
        self.store = StateStore(db)
        self.observer = Observer(cfg, state_store=self.store)
        # --fresh truncates JSONL ONCE at construction; the watch loop reuses
        # the same Snapshot so subsequent polls keep appending (4.1).
        self._fresh_done = False
        if fresh:
            self._truncate_jsonl()

    def _truncate_jsonl(self) -> None:
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.alerts_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.write_text("", encoding="utf-8")
        self.alerts_file.write_text("", encoding="utf-8")
        self._fresh_done = True

    def run(self, project_id: Optional[str] = None) -> Dict:
        projects = self.adapter.get_projects()
        pids = [project_id] if project_id else [p["id"] for p in projects]
        all_events: List[NormalizedEvent] = []
        for pid in pids:
            all_events += self._poll_project(pid)
        alerts: List[Alert] = []
        for ev in all_events:
            alerts += self.observer.feed(ev)
        self._persist(all_events, alerts)
        return {"events": all_events, "alerts": alerts}

    def _poll_project(self, pid: str) -> List[NormalizedEvent]:
        items = self.adapter.get_recent_events(pid, since=0)
        events: List[NormalizedEvent] = []
        # Two passes so turn timestamps exist before activities are mapped.
        turn_times: Dict[str, Dict[str, str]] = {}   # worker -> turnId -> time
        for item in items:
            if item["kind"] == "turn":
                turn = item["turn"]
                turn_times.setdefault(item["session_id"], {})[
                    str(turn.get("id"))] = \
                    turn.get("requestedAt") or turn.get("completedAt")
        for item in items:
            if item["kind"] == "session":
                events += self.normalizer.from_session(item["session"])
            elif item["kind"] == "turn":
                sid = item["session_id"]
                events += self.normalizer.from_turn(
                    sid, pid, item["turn"])
            elif item["kind"] == "activity":
                sid = item["session_id"]
                worker = next((s["session"] for s in items
                               if s["kind"] == "session"
                               and s["session"].get("id") == sid), {})
                fallback = (worker.get("activity") or {}).get("lastActivityAt")
                events += self.normalizer.from_activity(
                    sid, pid, item["activity"], turn_times.get(sid, {}),
                    fallback)
        return events

    def _persist(self, events: List[NormalizedEvent],
                 alerts: List[Alert]) -> None:
        for ev in events:
            _append_jsonl(self.events_file, ev.to_dict())
        for al in alerts:
            _append_jsonl(self.alerts_file, al.to_dict())


def _summary(result: Dict) -> None:
    events: List[NormalizedEvent] = result["events"]
    alerts: List[Alert] = result["alerts"]
    print("events: %d" % len(events))
    print("alerts: %d" % len(alerts))
    for al in alerts:
        print("ALERT [%s] %s" % (al.alert_type, al.description))


def _worker_meta(adapter: AOAdapter, pid: str) -> Dict[str, Dict]:
    return {w["id"]: w for w in adapter.get_workers(pid)}


def _sse_loop(snap: Snapshot, project_id: Optional[str]) -> None:
    """Feed live SSE payloads into the observer (best-effort attribution)."""
    cfg = snap.cfg
    pids = [project_id] if project_id else \
        [p["id"] for p in snap.adapter.get_projects()]
    meta: Dict[str, Dict[str, Dict]] = {}
    for pid in pids:
        try:
            meta[pid] = _worker_meta(snap.adapter, pid)
        except AOError:
            meta[pid] = {}
    for pid in pids:
        try:
            for payload in snap.adapter.stream_events(
                    pid, float(cfg["ao"].get("sse_idle_timeout_seconds", 30))):
                events = snap.normalizer.from_sse_payload(
                    payload, pid, meta[pid])
                for ev in events:
                    for al in snap.observer.feed(ev):
                        _append_jsonl(snap.alerts_file, al.to_dict())
                        print("ALERT [%s] %s" % (al.alert_type, al.description))
                    _append_jsonl(snap.events_file, ev.to_dict())
        except AOError as e:
            print("SSE stopped (%s); restarting loop" % e, file=sys.stderr)
            time.sleep(2)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ao-supervision-sidecar")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="run one snapshot and exit")
    mode.add_argument("--watch", action="store_true",
                      help="loop until interrupted")
    ap.add_argument("--config", default=None)
    ap.add_argument("--project", default=None,
                    help="restrict to one AO project id")
    ap.add_argument("--use-sse", action="store_true",
                    help="(--watch) also consume the SSE stream")
    ap.add_argument("--fresh", action="store_true",
                    help="truncate runtime jsonl files before writing")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config(args.config)
    snap = Snapshot(cfg, fresh=args.fresh)

    if args.once:
        result = snap.run(project_id=args.project)
        _summary(result)
        snap.store.close()
        return 0

    # --watch
    poll = float(cfg["ao"].get("poll_interval_seconds", 10))
    if args.use_sse:
        import threading
        threading.Thread(
            target=_sse_loop, args=(snap, args.project), daemon=True).start()
    print("watching (poll every %.0fs, Ctrl+C to stop)" % poll)
    try:
        while True:
            try:
                result = snap.run(project_id=args.project)
            except AOError as e:
                print("poll failed: %s" % e, file=sys.stderr)
                result = None
            if result and result["alerts"]:
                _summary(result)
            time.sleep(poll)
    except KeyboardInterrupt:
        print("stopped")
        snap.store.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
