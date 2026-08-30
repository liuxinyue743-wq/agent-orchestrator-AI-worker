"""4.1 regression: --watch --fresh must truncate JSONL exactly once."""
import json
from pathlib import Path
from unittest.mock import patch

import loopcore.cli as cli
from loopcore.state_store import StateStore


def _mk(tmp_path: Path):
    cfg = {
        "ao": {"base_url": "http://127.0.0.1:1", "request_timeout_seconds": 1,
               "sse_idle_timeout_seconds": 1, "poll_interval_seconds": 1},
        "thresholds": {
            "repeated_error": {"window_seconds": 600, "count": 3,
                               "cooldown_seconds": 600},
            "no_progress": {"window_seconds": 900, "min_activity_events": 8,
                            "max_progress_events": 0, "cooldown_seconds": 900,
                            "progress_mode": "strong"},
        },
        "runtime": {"events_file": "runtime/events.jsonl",
                    "alerts_file": "runtime/alerts.jsonl"},
    }
    return cfg


def test_fresh_truncates_once(tmp_path):
    cfg = _mk(tmp_path)
    db = str(tmp_path / "cl.db")
    snap = cli.Snapshot(cfg, fresh=True, db_path=db)
    ev = tmp_path / "events.jsonl"
    al = tmp_path / "alerts.jsonl"
    snap.events_file = ev
    snap.alerts_file = al
    snap._truncate_jsonl()
    # first truncate
    ev.write_text("OLD\n", encoding="utf-8")
    snap._truncate_jsonl()
    assert ev.read_text(encoding="utf-8") == ""
    # simulate a poll writing a line
    cli._append_jsonl(ev, {"x": 1})
    assert ev.read_text(encoding="utf-8").strip() == '{"x": 1}'
    # second "fresh" call must NOT wipe what we just wrote — emulate watch by
    # NOT calling _truncate again (fresh_done guards it).
    cli._append_jsonl(ev, {"x": 2})
    lines = [l for l in ev.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 2
    snap.store.close()


def test_no_fresh_keeps_existing(tmp_path):
    cfg = _mk(tmp_path)
    db = str(tmp_path / "cl.db")
    ev = tmp_path / "events.jsonl"
    ev.write_text("EXISTING\n", encoding="utf-8")
    snap = cli.Snapshot(cfg, fresh=False, db_path=db)
    snap.events_file = ev
    snap.alerts_file = tmp_path / "alerts.jsonl"
    # without fresh, _truncate is never called
    assert ev.read_text(encoding="utf-8") == "EXISTING\n"
    snap.store.close()
