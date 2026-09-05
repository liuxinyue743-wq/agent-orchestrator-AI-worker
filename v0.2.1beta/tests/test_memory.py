"""Offline tests for ProjectMemory: Planner-authored, Bus-written files."""

from __future__ import annotations

import pytest

from loopcore.envelope import Envelope, MessageKind
from loopcore.memory import MemoryError, MemoryEntry, ProjectMemory


@pytest.fixture
def mem(tmp_path):
    return ProjectMemory(tmp_path)


def update_envelope(updates):
    return Envelope(
        sender="planner",
        receiver="bus",
        kind=MessageKind.MEMORY_UPDATE,
        thread_id="M1",
        payload={"updates": updates},
    )


class TestWrite:
    def test_creates_both_files_with_headers(self, mem):
        mem.handle_envelope(update_envelope([
            {"target": "project", "heading": "MISSION-001 拆解", "body": "S1 divide"},
            {"target": "memory", "heading": "失败模式", "body": "GLM 抖动"},
        ]))
        assert mem.project_path.exists() and mem.memory_path.exists()
        assert "重大事项" in mem.read("project")
        assert "项目记忆" in mem.read("memory")

    def test_entries_appended_in_order(self, mem):
        mem.append(MemoryEntry(target="project", heading="一", body="1"))
        mem.append(MemoryEntry(target="project", heading="二", body="2"))
        text = mem.read("project")
        assert text.index("## 一") < text.index("## 二")

    def test_validation(self, mem):
        with pytest.raises(ValueError):
            MemoryEntry(target="notes", heading="h", body="b")
        with pytest.raises(ValueError):
            MemoryEntry(target="memory", heading=" ", body="b")

    def test_empty_updates_rejected(self, mem):
        with pytest.raises(MemoryError):
            mem.handle_envelope(update_envelope([]))

    def test_wrong_kind_rejected(self, mem):
        e = Envelope(sender="planner", receiver="bus",
                     kind=MessageKind.MEMORY_UPDATE, thread_id="M1",
                     payload={"updates": [{"target": "memory", "heading": "h", "body": "b"}]})
        object.__setattr__(e, "kind", MessageKind.AUDIT_REQUEST)
        with pytest.raises(MemoryError):
            mem.handle_envelope(e)

    def test_missing_root_fails_closed(self, tmp_path):
        with pytest.raises(MemoryError):
            ProjectMemory(tmp_path / "does-not-exist")

    def test_read_missing_file_is_empty(self, mem):
        assert mem.read("project") == ""
        with pytest.raises(ValueError):
            mem.read("notes")
