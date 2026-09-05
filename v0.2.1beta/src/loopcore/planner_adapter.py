"""Planner adapter.

Providers:
  FakePlannerProvider              - deterministic, for unit tests.
  CodexCliPlannerProvider          - production planner via the shared Codex
                                     CLI structured-output boundary.

The Planner is a planning agent (no code editing): it maps an AuditResult to a
PlannerAction (CONTINUE/SEND_LOCAL_FIX/REPLAN_SPAWN/CANDIDATE_DONE/HUMAN).

On two invalid outputs -> HUMAN. Never substitutes FakePlanner for a real run.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .codex_cli import run_codex_json
from .mission_contracts import (AuditResult, PlannerAction, PlannerActionType,
                        AuditDecision, validate_planner_action, validate_mission_plan)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
SCHEMA_DIR = PROMPT_DIR.parent / "schemas"


class PlannerProvider:
    def plan(self, audit: AuditResult, task_spec_dict: dict,
             action_id: str, *, target_session_id: Optional[str] = None,
             remaining_replans: int = 0,
             instruct: str = "",
             board: Optional[dict] = None) -> PlannerAction:
        raise NotImplementedError

    def plan_decompose(self, mission: dict, plan_id: str) -> "MissionPlan":
        """Leader capability: decompose ONE mission into parallel subtasks."""
        raise NotImplementedError


class FakePlannerProvider(PlannerProvider):
    """Deterministic mapping for tests."""
    def plan(self, audit: AuditResult, task_spec_dict: dict,
             action_id: str, *, target_session_id: Optional[str] = None,
             remaining_replans: int = 0,
             instruct: str = "",
             board: Optional[dict] = None) -> PlannerAction:
        self.last_board = board
        d = audit.decision
        if d == AuditDecision.PASS:
            return PlannerAction(action_id=action_id, task_id=audit.task_id,
                action=PlannerActionType.CANDIDATE_DONE, reason="audit PASS")
        if d == AuditDecision.LOCAL_FIX:
            return PlannerAction(action_id=action_id, task_id=audit.task_id,
                action=PlannerActionType.SEND_LOCAL_FIX,
                target_session_id=target_session_id,
                message=audit.recommended_action or "Fix the failing acceptance criteria.",
                reason="audit LOCAL_FIX")
        if d == AuditDecision.REPLAN:
            if remaining_replans > 0:
                return PlannerAction(action_id=action_id, task_id=audit.task_id,
                    action=PlannerActionType.REPLAN_SPAWN,
                    replacement_task_spec=task_spec_dict, reason="audit REPLAN")
            return PlannerAction(action_id=action_id, task_id=audit.task_id,
                action=PlannerActionType.HUMAN, reason="replans exhausted")
        return PlannerAction(action_id=action_id, task_id=audit.task_id,
            action=PlannerActionType.HUMAN, reason="audit HUMAN")

    def plan_decompose(self, mission: dict, plan_id: str) -> "MissionPlan":
        """Deterministic 2-way split for tests: first allowed path vs rest."""
        from .mission_contracts import SubtaskPlan, MissionPlan, AcceptanceCriterion
        mission_id = mission.get("mission_id", "M1")
        acs = mission.get("acceptance_criteria") or []
        paths = mission.get("allowed_paths") or ["app.py"]
        # split ACs evenly across two subtasks with disjoint path guesses
        half = max(1, len(acs) // 2)
        sub1 = SubtaskPlan(
            subtask_id="%s-S1" % mission_id,
            objective="Part 1: %s" % mission.get("objective", ""),
            allowed_paths=paths[:1],
            acceptance_criteria=[
                AcceptanceCriterion(**a) for a in acs[:half]])
        sub2 = SubtaskPlan(
            subtask_id="%s-S2" % mission_id,
            objective="Part 2: %s" % mission.get("objective", ""),
            allowed_paths=paths[1:] or paths[:1],
            acceptance_criteria=[
                AcceptanceCriterion(**a) for a in acs[half:]],
            dependencies=["%s-S1" % mission_id])
        return MissionPlan(mission_id=mission_id, subtasks=[sub1, sub2],
                           strategy="fake decompose")


def _coerce_planner_strings(obj: dict) -> None:
    """Normalize common real-model output quirks before schema validation.

    Live planners can emit `"message": null` or
    omit `plan` when the action needs no message; the schema declares those
    as plain strings, so validation failed and a healthy PASS decision was
    degraded to the HUMAN fallback. Coerce None -> "" (and drop non-scalar
    junk) instead of rejecting the whole action.
    """
    for key in ("message", "reason", "plan", "action_id", "task_id"):
        v = obj.get(key)
        if v is None:
            obj[key] = ""
        elif not isinstance(v, str):
            obj[key] = json.dumps(v, ensure_ascii=False)


class CodexCliPlannerProvider(PlannerProvider):
    """Production Planner using ephemeral, read-only Codex CLI calls."""

    def __init__(self, *, codex_bin: str = "codex", timeout: int = 180,
                 model: Optional[str] = None, cwd: Optional[Path] = None,
                 **legacy_options):
        # The alias below keeps older compatibility CLIs importable. Their AO
        # connection arguments never belonged to the headless Planner and are
        # intentionally ignored; no Claude implementation remains here.
        unknown = set(legacy_options) - {
            "ao_bin", "project_id", "data_dir", "run_file",
        }
        if unknown:
            raise TypeError("unexpected Planner options: %s"
                            % ", ".join(sorted(unknown)))
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.model = model or "gpt-5.6-sol"
        self.cwd = Path(cwd) if cwd is not None else PROMPT_DIR.parent
        self.system_prompt = (PROMPT_DIR / "planner.md").read_text("utf-8")
        self.decompose_prompt = (PROMPT_DIR / "planner-decompose.md").read_text(
            "utf-8")
        self.action_schema_path = SCHEMA_DIR / "planner-action.schema.json"
        self.mission_schema_path = SCHEMA_DIR / "mission-plan.schema.json"

    def _run(self, prompt: str, schema_path: Path) -> dict:
        return run_codex_json(
            prompt=prompt,
            schema_path=schema_path,
            model=self.model,
            timeout=self.timeout,
            codex_bin=self.codex_bin,
            cwd=self.cwd,
        )

    def _call(self, audit: AuditResult, task_spec_dict: dict, action_id: str,
              *, target_session_id: Optional[str], remaining_replans: int,
              instruct: str = "", board: Optional[dict] = None) -> dict:
        task_input = json.dumps({
            "action_id": action_id,
            "task_id": audit.task_id,
            "audit_result": audit.to_dict(),
            "task_spec": task_spec_dict,
            "target_session_id": target_session_id,
            "remaining_replans": remaining_replans,
            "user_instruction": instruct or "",
            "mission_board": board or None,
            "instruction": ("Output ONLY a PlannerAction JSON object matching "
                            "the schema, with action_id=%s." % action_id),
        }, ensure_ascii=False, indent=2)
        prompt = "%s\n\n# Task input\n%s" % (self.system_prompt, task_input)
        return self._run(prompt, self.action_schema_path)

    def plan(self, audit: AuditResult, task_spec_dict: dict,
             action_id: str, *, target_session_id: Optional[str] = None,
             remaining_replans: int = 0,
             instruct: str = "",
             board: Optional[dict] = None) -> PlannerAction:
        last_err = ""
        for attempt in range(2):
            try:
                obj = self._call(audit, task_spec_dict, action_id,
                                 target_session_id=target_session_id,
                                 remaining_replans=remaining_replans,
                                 instruct=instruct,
                                 board=board)
                obj.setdefault("action_id", action_id)
                obj.setdefault("task_id", audit.task_id)
                _coerce_planner_strings(obj)
                ok, msg = validate_planner_action(obj)
                if obj.get("action") == PlannerActionType.REPLAN_SPAWN:
                    replacement = obj.get("replacement_task_spec")
                    objective = replacement.get("objective") \
                        if isinstance(replacement, dict) else None
                    if not isinstance(objective, str) or not objective.strip():
                        ok = False
                        msg = ("REPLAN_SPAWN requires replacement_task_spec."
                               "objective to be a non-empty string")
                    else:
                        replacement["objective"] = objective.strip()
                if ok:
                    return PlannerAction.from_dict(obj)
                last_err = "schema: %s" % msg
            except Exception as e:  # noqa
                last_err = "call: %s" % e
            if attempt == 0:
                time.sleep(0.2)
        return PlannerAction(action_id=action_id, task_id=audit.task_id,
            action=PlannerActionType.HUMAN,
            reason="planner invalid output: %s" % last_err)


    # ---------------------------------------------------- decomposition
    def plan_decompose(self, mission: dict, plan_id: str) -> "MissionPlan":
        """Leader capability: decompose ONE mission into parallel subtasks.

        A separate call from per-cycle plan(): different output shape (a
        MissionPlan with subtasks, not a single action). Schema-validated
        against mission-plan; two attempts; both fail -> RuntimeError (the
        controller's existing boundary halts to HUMAN).
        """
        from .mission_contracts import MissionPlan
        max_sub = int((mission.get("budgets") or {})
                      .get("max_subtasks", 1))
        if max_sub < 1:
            raise ValueError("budgets.max_subtasks must be positive")
        last_err = ""
        for attempt in range(2):
            try:
                obj = self._call_decompose(mission, plan_id, max_sub)
                ok, msg = self._validate_mission_plan(obj, max_sub)
                if ok and obj.get("mission_id") != mission.get("mission_id"):
                    ok = False
                    msg = "mission_id does not match the input mission"
                if ok:
                    return MissionPlan.from_dict(obj)
                last_err = "schema: %s" % msg
            except Exception as e:  # noqa
                last_err = "call: %s" % e
            if attempt == 0:
                time.sleep(0.2)
        raise RuntimeError("planner decompose failed twice: %s" % last_err)

    @staticmethod
    def _decompose_instruction(max_sub: int) -> str:
        if max_sub <= 1:
            head = ("Decompose this mission into EXACTLY 1 subtask (a "
                    "trivial decomposition: one worker lane executes the "
                    "whole mission) ")
        else:
            head = ("Return 1..%d subtasks. Prefer EXACTLY 1 subtask. Use a "
                    "second worker only when allowed_paths split naturally "
                    "without overlap, acceptance criteria are independent, "
                    "there is no strong cross-task dependency, and it gives "
                    "real parallel benefit. If the mission is small, changes "
                    "the same file, is strongly coupled, or splitting only "
                    "adds merge cost, you MUST return 1 subtask " % max_sub)
        return (head +
                "and output ONLY a MissionPlan JSON object. Prefer DISJOINT "
                "allowed_paths across subtasks (avoids merge conflicts); "
                "use dependencies only when B truly needs A's output. Each "
                "subtask MUST carry gate_commands scoped to its OWN files "
                "(workers run in isolated worktrees and cannot see sibling "
                "subtasks' output, so the mission-wide gate would fail "
                "there; the system runs the full gate on the merged tree "
                "at the end).")

    def _call_decompose(self, mission: dict, plan_id: str,
                        max_sub: int) -> dict:
        task_input = json.dumps({
            "mission": mission,
            "plan_id": plan_id,
            "max_subtasks": max_sub,
            "instruction": self._decompose_instruction(max_sub),
        }, ensure_ascii=False, indent=2)
        prompt = "%s\n\n# Mission input\n%s" % (
            self.decompose_prompt, task_input)
        return self._run(prompt, self.mission_schema_path)

    @staticmethod
    def _validate_mission_plan(obj: dict, max_sub: int):
        from .mission_contracts import MissionPlan
        if not isinstance(obj, dict):
            return False, "not an object"
        ok, detail = validate_mission_plan(obj)
        if not ok:
            return False, detail
        subs = obj.get("subtasks")
        if not isinstance(subs, list) or not (1 <= len(subs) <= max_sub):
            return False, ("subtasks must be a list of 1..%d" % max_sub)
        ids = set()
        for s in subs:
            if not isinstance(s, dict) or not s.get("subtask_id"):
                return False, "subtask missing subtask_id"
            if s["subtask_id"] in ids:
                return False, "duplicate subtask_id"
            ids.add(s["subtask_id"])
            if not s.get("objective"):
                return False, "subtask missing objective"
            if not s.get("allowed_paths"):
                return False, "subtask missing allowed_paths"
        obj.setdefault("mission_id", "")
        obj.setdefault("strategy", "")
        try:
            MissionPlan.from_dict(obj)
        except Exception as e:  # noqa
            return False, "unparsable: %s" % e
        return True, ""


# Compatibility for the legacy CLI modules that are outside this migration's
# allowed edit scope.  There is only one production Planner implementation.
AOOrchestratorPlannerProvider = CodexCliPlannerProvider
