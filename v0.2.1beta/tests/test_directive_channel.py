"""Directive channel routing (owner rule 2026-08-30): the user may address
ANY agent mid-mission; planner directives stay private, everything else is
mirrored into the planner's instruct. Each target must receive the text
through its REAL input path (instruct / worker send / bundle history /
verifier notes)."""

from __future__ import annotations

from unittest.mock import MagicMock

from loopcore.mission import MissionController
from tests.sidecar_port.test_mission import _mc


def _loops(mc):
    mc.step()          # decompose -> loops exist
    return mc.loops


def test_planner_directive_goes_to_instruct_only(tmp_path):
    mc, store = _mc(tmp_path)
    loops = _loops(mc)
    mc.executor.nudge_worker = MagicMock()
    mc.directives.post("planner", "优先保证边界条件测试")
    mc._apply_directives()
    for loop in loops.values():
        assert "优先保证边界条件测试" in loop.instruct
        assert "[用户指令" in loop.instruct
        # planner directive does NOT leak into auditor/verifier channels
        assert loop.role_directives["auditor"] == []
        assert loop.role_directives["verifier"] == []
    mc.executor.nudge_worker.assert_not_called()


def test_worker_directive_sent_and_mirrored(tmp_path):
    mc, store = _mc(tmp_path)
    loops = _loops(mc)
    mc.executor.nudge_worker = MagicMock(return_value=True)
    mc.directives.post("worker:closed-loop-demo-99", "改用迭代实现")
    mc._apply_directives()
    mc.executor.nudge_worker.assert_called_once()
    args = mc.executor.nudge_worker.call_args.args
    assert args[0] == "closed-loop-demo-99"
    assert "改用迭代实现" in args[1]
    # visibility: planner sees a mirror copy
    for loop in loops.values():
        assert "镜像·发给 worker:closed-loop-demo-99" in loop.instruct
        assert "改用迭代实现" in loop.instruct


def test_auditor_and_verifier_directives_land_in_role_channels(tmp_path):
    mc, store = _mc(tmp_path)
    loops = _loops(mc)
    mc.directives.post("auditor", "重点审查除零分支")
    mc.directives.post("verifier", "确认没有空实现")
    mc._apply_directives()
    for loop in loops.values():
        assert any("重点审查除零分支" in d
                   for d in loop.role_directives["auditor"])
        assert any("确认没有空实现" in d
                   for d in loop.role_directives["verifier"])
        # mirrored to planner as well
        assert "镜像·发给 auditor" in loop.instruct
        assert "镜像·发给 verifier" in loop.instruct


def test_drain_is_once_only(tmp_path):
    mc, store = _mc(tmp_path)
    _loops(mc)
    mc.directives.post("planner", "只应出现一次")
    mc._apply_directives()
    assert mc.directives.pending_count() == 0
    before = [loop.instruct for loop in mc.loops.values()]
    mc._apply_directives()   # nothing left -> unchanged
    assert before == [loop.instruct for loop in mc.loops.values()]
