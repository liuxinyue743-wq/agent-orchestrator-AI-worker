"""Project memory files: memory.md and project.md, written for the Planner.

Design baseline: ARCHITECTURE-v0.2.md section 3.
The Planner agent generates update entries; this deterministic writer
atomically appends them, so agent output is never lost to a partial write.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .envelope import Envelope, MessageKind


class MemoryError(RuntimeError):
    """Deterministic memory-write failure; never guess a partial state."""


@dataclass(frozen=True)
class MemoryEntry:
    """One Planner-authored update destined for memory.md or project.md."""

    target: str  # "memory" | "project"
    heading: str
    body: str

    def __post_init__(self) -> None:
        if self.target not in ("memory", "project"):
            raise ValueError("target must be 'memory' or 'project'")
        if not self.heading.strip():
            raise ValueError("heading must be non-blank")
        if not self.body.strip():
            raise ValueError("body must be non-blank")


class ProjectMemory:
    """Owns memory.md / project.md inside the target project root."""

    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        root = Path(project_root)
        if not root.is_dir():
            raise MemoryError(f"project root does not exist: {root}")
        self._root = root
        self._lock = threading.Lock()

    @property
    def memory_path(self) -> Path:
        return self._root / "memory.md"

    @property
    def project_path(self) -> Path:
        return self._root / "project.md"

    def handle_envelope(self, envelope: Envelope) -> list[MemoryEntry]:
        """Bus handler for Planner MEMORY_UPDATE envelopes.

        Payload shape:
            {"updates": [{"target": "memory"|"project",
                          "heading": str, "body": str}, ...]}
        Returns the entries actually appended.
        """

        if envelope.kind is not MessageKind.MEMORY_UPDATE:
            raise MemoryError(f"unsupported kind for memory writer: {envelope.kind}")
        updates = envelope.payload.get("updates")
        if not isinstance(updates, list) or not updates:
            raise MemoryError("MEMORY_UPDATE payload.updates must be a non-empty list")
        entries = [
            MemoryEntry(
                target=str(u.get("target", "")),
                heading=str(u.get("heading", "")),
                body=str(u.get("body", "")),
            )
            for u in updates
        ]
        for entry in entries:
            self.append(entry)
        return entries

    def append(self, entry: MemoryEntry) -> None:
        """Atomically append one entry (tmp file + os.replace for the header;
        append-mode write for the body, guarded by the process lock)."""

        path = self.memory_path if entry.target == "memory" else self.project_path
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        block = f"\n## {entry.heading}\n\n_{timestamp} · {entry.target}.md_\n\n{entry.body.rstrip()}\n"
        with self._lock:
            if not path.exists():
                title = "# memory.md — 项目记忆\n" if entry.target == "memory" else "# project.md — 重大事项与进展\n"
                self._atomic_write(path, title)
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(block)

    def read(self, target: str) -> str:
        path = self.memory_path if target == "memory" else self.project_path
        if target not in ("memory", "project"):
            raise ValueError("target must be 'memory' or 'project'")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
