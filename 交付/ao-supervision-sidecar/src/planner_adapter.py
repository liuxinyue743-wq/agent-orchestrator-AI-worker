"""Planner adapter.

Providers:
  FakePlannerProvider              - deterministic, for unit tests.
  AOOrchestratorPlannerProvider    - real planner via Claude CLI headless
                                     (GLM-5.2), reading the system prompt and
                                     emitting a validated PlannerAction.

The Planner is a planning agent (no code editing): it maps an AuditResult to a
PlannerAction (CONTINUE/SEND_LOCAL_FIX/REPLAN_SPAWN/CANDIDATE_DONE/HUMAN).

On two invalid outputs -> HUMAN. Never substitutes FakePlanner for a real run.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .contracts import (AuditResult, PlannerAction, PlannerActionType,
                        AuditDecision, validate_planner_action)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


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
        from .contracts import SubtaskPlan, MissionPlan, AcceptanceCriterion
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


def _resolve_exe(bin_name: str) -> str:
    if os.name != "nt":
        return bin_name
    if Path(bin_name).exists():
        return bin_name
    import shutil
    resolved = shutil.which(bin_name)
    if resolved:
        return resolved
    npm_dir = Path.home() / "AppData" / "Roaming" / "npm"
    for cand in (npm_dir / (bin_name + ".cmd"), npm_dir / bin_name):
        if cand.exists():
            return str(cand)
    return bin_name


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _extract_json(text: str):
    import re
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def _coerce_planner_strings(obj: dict) -> None:
    """Normalize common real-model output quirks before schema validation.

    Live planners (GLM via claude -p) frequently emit `"message": null` or
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


class AOOrchestratorPlannerProvider(PlannerProvider):
    """Real Planner via Claude CLI headless (GLM-5.2), no tools.

    Uses `claude -p --system-prompt-file <planner.md> --output-format json
    --json-schema <planner-action> --disallowedTools "*" --max-budget-usd N`.
    The AuditResult + TaskSpec go via STDIN. The assistant must emit a
    PlannerAction JSON object.

    This deliberately does NOT use `ao spawn --kind orchestrator`: that path
    inherits the AO daemon's environment (ANTHROPIC_MODEL etc.), which can
    point at a model the team is not allowed to access (403). Calling the CLI
    directly lets us override ANTHROPIC_MODEL to a known-working value, the
    same way the Auditor does.
    """
    def __init__(self, ao_bin: str = "", project_id: str = "",
                 data_dir: str = "", run_file: str = "",
                 claude_bin: str = "claude",
                 budget_usd: float = 0.20, timeout: int = 180,
                 model: Optional[str] = None):
        # ao_bin/data_dir/run_file kept for backward CLI compatibility but
        # are not used by the direct-CLI planner.
        self.ao_bin = ao_bin
        self.project_id = project_id
        self.data_dir = data_dir
        self.run_file = run_file
        self.bin = _resolve_exe(claude_bin)
        self.budget = budget_usd
        self.timeout = timeout
        self.model = model or os.environ.get("ANTHROPIC_MODEL_PLANNER",
                                              "GLM-5.2")
        with open(PROMPT_DIR / "planner.md", encoding="utf-8") as f:
            self.system_prompt = f.read()
        with open(PROMPT_DIR.parent / "schemas" / "planner-action.schema.json",
                  encoding="utf-8") as f:
            self.schema = json.load(f)
        self._sp_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        self._sp_file.write(self.system_prompt)
        self._sp_file.flush()
        self._sp_path = self._sp_file.name

    def _env(self) -> dict:
        e = dict(os.environ)
        e["ANTHROPIC_MODEL"] = self.model
        return e

    def _call(self, audit: AuditResult, task_spec_dict: dict, action_id: str,
              *, target_session_id: Optional[str], remaining_replans: int,
              instruct: str = "", board: Optional[dict] = None,
              use_schema: bool = True) -> dict:
        prompt = json.dumps({
            "action_id": action_id, "task_id": audit.task_id,
            "audit_result": audit.to_dict(),
            "task_spec": task_spec_dict,
            "target_session_id": target_session_id,
            "remaining_replans": remaining_replans,
            "user_instruction": instruct or "",
            "mission_board": (board or None),
            "instruction": ("Output ONLY a PlannerAction JSON object matching "
                            "the schema, with action_id=%s." % action_id),
        }, ensure_ascii=False, indent=2)
        cmd = [self.bin, "-p",
               "--system-prompt-file", self._sp_path,
               "--output-format", "json",
               "--disallowedTools", "*",
               "--max-budget-usd", str(self.budget)]
        if use_schema:
            cmd += ["--json-schema", json.dumps(self.schema)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=self.timeout,
                              encoding="utf-8", errors="replace",
                              env=self._env())
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError("claude empty stdout rc=%s stderr=%s"
                               % (proc.returncode, (proc.stderr or "")[:300]))
        wrapper = json.loads(out)
        if isinstance(wrapper, dict) and wrapper.get("is_error"):
            raise RuntimeError("claude error subtype=%s"
                               % wrapper.get("subtype"))
        if isinstance(wrapper, dict) and "result" in wrapper:
            inner = wrapper["result"]
            if isinstance(inner, str):
                inner = _extract_json(inner) or json.loads(_strip_fences(inner))
            return inner
        return wrapper

    def plan(self, audit: AuditResult, task_spec_dict: dict,
             action_id: str, *, target_session_id: Optional[str] = None,
             remaining_replans: int = 0,
             instruct: str = "",
             board: Optional[dict] = None) -> PlannerAction:
        last_err = ""
        for attempt, use_schema in ((0, True), (1, False)):
            try:
                obj = self._call(audit, task_spec_dict, action_id,
                                 target_session_id=target_session_id,
                                 remaining_replans=remaining_replans,
                                 instruct=instruct,
                                 board=board,
                                 use_schema=use_schema)
                obj.setdefault("action_id", action_id)
                obj.setdefault("task_id", audit.task_id)
                _coerce_planner_strings(obj)
                ok, msg = validate_planner_action(obj)
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
        against mission-plan; two attempts; both fail -> empty MissionPlan
        (the controller halts to HUMAN — no runaway decomposition).
        """
        from .contracts import MissionPlan
        max_sub = int((mission.get("budgets") or {}).get("max_subtasks", 5)
                      or 5)
        last_err = ""
        for attempt, use_schema in ((0, True), (1, False)):
            try:
                obj = self._call_decompose(mission, plan_id, max_sub,
                                           use_schema=use_schema)
                ok, msg = self._validate_mission_plan(obj, max_sub)
                if ok:
                    return MissionPlan.from_dict(obj)
                last_err = "schema: %s" % msg
            except Exception as e:  # noqa
                last_err = "call: %s" % e
            if attempt == 0:
                time.sleep(0.2)
        raise RuntimeError("planner decompose failed twice: %s" % last_err)

    def _call_decompose(self, mission: dict, plan_id: str, max_sub: int,
                        use_schema: bool = True) -> dict:
        with open(PROMPT_DIR.parent / "schemas" / "mission-plan.schema.json",
                  encoding="utf-8") as f:
            mschema = json.load(f)
        prompt = json.dumps({
            "mission": mission,
            "plan_id": plan_id,
            "max_subtasks": max_sub,
            "instruction": ("Decompose this mission into 2..%d parallel "
                            "subtasks and output ONLY a MissionPlan JSON "
                            "object. Prefer DISJOINT allowed_paths across "
                            "subtasks (avoids merge conflicts); use "
                            "dependencies only when B truly needs A's output. "
                            "Each subtask MUST carry gate_commands scoped to "
                            "its OWN files (workers run in isolated worktrees "
                            "and cannot see sibling subtasks' output, so the "
                            "mission-wide gate would fail there; the system "
                            "runs the full gate on the merged tree at the end)."
                            % max_sub),
        }, ensure_ascii=False, indent=2)
        cmd = [self.bin, "-p",
               "--system-prompt-file",
               self._decompose_sp_path(),
               "--output-format", "json",
               "--disallowedTools", "*",
               "--max-budget-usd", str(self.budget)]
        if use_schema:
            cmd += ["--json-schema", json.dumps(mschema)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=self.timeout,
                              encoding="utf-8", errors="replace",
                              env=self._env())
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError("claude empty stdout rc=%s stderr=%s"
                               % (proc.returncode, (proc.stderr or "")[:300]))
        wrapper = json.loads(out)
        if isinstance(wrapper, dict) and wrapper.get("is_error"):
            raise RuntimeError("claude error subtype=%s"
                               % wrapper.get("subtype"))
        if isinstance(wrapper, dict) and "result" in wrapper:
            inner = wrapper["result"]
            if isinstance(inner, str):
                inner = _extract_json(inner) or json.loads(
                    _strip_fences(inner))
            return inner
        return wrapper

    def _decompose_sp_path(self) -> str:
        if self.__dict__.get("_dsp"):
            return self._dsp
        import tempfile
        with open(PROMPT_DIR / "planner-decompose.md", encoding="utf-8") as f:
            sp = f.read()
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                         delete=False, encoding="utf-8")
        tf.write(sp)
        tf.flush()
        self._dsp = tf.name
        return self._dsp

    @staticmethod
    def _validate_mission_plan(obj: dict, max_sub: int):
        from .contracts import MissionPlan
        if not isinstance(obj, dict):
            return False, "not an object"
        subs = obj.get("subtasks")
        if not isinstance(subs, list) or not (2 <= len(subs) <= max_sub):
            return False, ("subtasks must be a list of 2..%d" % max_sub)
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
