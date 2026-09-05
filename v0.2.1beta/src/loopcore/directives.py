"""User directive channel: the panel (or any operator UI) posts directives
mid-mission; the MissionController drains them once per tick and routes each
to its target's REAL input path:

  planner        -> folded into every loop's `instruct` (the Planner reads
                    it on the very next plan() call)
  worker:<sid>   -> sent to the worker session via the executor (the real
                    AO chat channel), AND mirrored into `instruct` so the
                    Planner sees it (owner-ruled visibility)
  auditor        -> appended to the next EvidenceBundle history
  verifier       -> appended to the next VerifierInput.user_notes
  observer/gate  -> deterministic programs with no semantic input; recorded
                    and mirrored to the Planner only

Visibility rule (owner 2026-08-30): directives addressed to the Planner stay
private; directives to any other endpoint are ALWAYS mirrored to the
Planner. The bus envelope layer mirrors for traffic; THIS channel mirrors
for actual consumption.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List

from .event_normalizer import now_iso


@dataclass
class Directive:
    target: str            # planner | auditor | verifier | observer | gate
                           # | worker:<session_id>
    text: str
    at: str = field(default_factory=now_iso)


class DirectiveChannel:
    """Thread-safe pending queue. Producer: the web panel handler thread.
    Consumer: the MissionController tick."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: List[Directive] = []

    def post(self, target: str, text: str) -> Directive:
        d = Directive(target=(target or "").strip(), text=(text or "").strip())
        with self._lock:
            self._pending.append(d)
        return d

    def drain(self) -> List[Directive]:
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
