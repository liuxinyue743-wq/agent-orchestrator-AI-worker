"""Mission-level contracts: MissionSpec/MissionPlan/TaskSpec, AuditResult,
PlannerAction, VerifierResult + the task state machine.

Ported from ao-supervision-sidecar src/contracts.py (same team), with the
schema root corrected for the closed-loop-v2 layout. Production validation
always uses JSON Schema; a missing validator is a startup error, not a
reason to weaken the contract. Local validation is independent of the model
provider's structured-output feature.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# src/loopcore/mission_contracts.py -> closed-loop-v2/ (holds schemas/)
ROOT = Path(__file__).resolve().parent.parent.parent

try:
    import jsonschema
except ImportError as exc:
    raise RuntimeError(
        "CL_AO requires jsonschema for full contract validation. "
        "Run bootstrap.ps1, or install this product's requirements.txt "
        "with the same Python interpreter. Validation will not be bypassed."
    ) from exc


def _load_schema(name: str) -> Dict:
    with open(ROOT / "schemas" / ("%s.schema.json" % name), encoding="utf-8") as f:
        return json.load(f)


_SCHEMAS = {n: _load_schema(n) for n in
            ("task-spec", "audit-result", "planner-action", "project-state",
             "verifier-result", "mission-plan")}


def _build_validators() -> Dict:
    validators = {}
    for name, schema in _SCHEMAS.items():
        cls = jsonschema.validators.validator_for(schema)
        # Check schemas once at startup; malformed schemas must not silently
        # disable validation. Reuse compiled validators on normal calls.
        cls.check_schema(schema)
        validators[name] = cls(schema)
    return validators


_VALIDATORS = _build_validators()


def _validate(obj: Dict, schema_name: str) -> Tuple[bool, str]:
    # Python's JSON parser permits NaN/Infinity by default. They are not JSON
    # numbers and can evade comparisons such as minimum/maximum on NaN.
    try:
        json.dumps(obj, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False, "input must be a JSON value with finite numbers"
    error = next(_VALIDATORS[schema_name].iter_errors(obj), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "$"
        # Do not echo the full payload (which may contain user code) into logs.
        return False, "%s: schema %s constraint failed" % (
            location, error.validator)
    return True, ""


# ----------------------------------------------------------------- enums
class AuditDecision:
    PASS = "PASS"
    LOCAL_FIX = "LOCAL_FIX"
    REPLAN = "REPLAN"
    HUMAN = "HUMAN"


class PlannerActionType:
    CONTINUE = "CONTINUE"
    SEND_LOCAL_FIX = "SEND_LOCAL_FIX"
    REPLAN_SPAWN = "REPLAN_SPAWN"
    CANDIDATE_DONE = "CANDIDATE_DONE"
    HUMAN = "HUMAN"


class ProjectState:
    TASK_READY = "TASK_READY"
    WORKER_RUNNING = "WORKER_RUNNING"
    AUDIT_PENDING = "AUDIT_PENDING"
    PLANNER_PENDING = "PLANNER_PENDING"
    LOCAL_FIX_PENDING = "LOCAL_FIX_PENDING"
    WORKER_RETRYING = "WORKER_RETRYING"
    REPLAN_PENDING = "REPLAN_PENDING"
    GATE_PENDING = "GATE_PENDING"
    VERIFIER_PENDING = "VERIFIER_PENDING"
    DONE = "DONE"
    HUMAN = "HUMAN"
    FAILED = "FAILED"


# Legal state transitions (no arbitrary jumps).
# New task gates transition directly from GATE_PENDING to DONE on PASS.
# VERIFIER_PENDING and its transitions remain legal so runtimes persisted by
# earlier versions can resume their in-flight task verifier safely.
LEGAL_TRANSITIONS: Dict[str, set] = {
    ProjectState.TASK_READY: {ProjectState.WORKER_RUNNING, ProjectState.HUMAN,
                              ProjectState.FAILED},
    ProjectState.WORKER_RUNNING: {ProjectState.AUDIT_PENDING, ProjectState.GATE_PENDING,
                                  ProjectState.HUMAN, ProjectState.FAILED},
    ProjectState.AUDIT_PENDING: {ProjectState.PLANNER_PENDING, ProjectState.HUMAN},
    ProjectState.PLANNER_PENDING: {ProjectState.LOCAL_FIX_PENDING,
                                   ProjectState.REPLAN_PENDING,
                                   ProjectState.GATE_PENDING,
                                   ProjectState.WORKER_RUNNING,
                                   ProjectState.HUMAN},
    ProjectState.LOCAL_FIX_PENDING: {ProjectState.WORKER_RETRYING, ProjectState.HUMAN},
    ProjectState.WORKER_RETRYING: {ProjectState.AUDIT_PENDING, ProjectState.GATE_PENDING,
                                   ProjectState.HUMAN},
    ProjectState.REPLAN_PENDING: {ProjectState.WORKER_RUNNING, ProjectState.HUMAN,
                                  ProjectState.FAILED},
    ProjectState.GATE_PENDING: {ProjectState.DONE,
                                ProjectState.VERIFIER_PENDING,
                                ProjectState.AUDIT_PENDING,
                                ProjectState.HUMAN, ProjectState.FAILED},
    ProjectState.VERIFIER_PENDING: {ProjectState.DONE, ProjectState.AUDIT_PENDING,
                                    ProjectState.HUMAN},
    ProjectState.DONE: set(),
    ProjectState.HUMAN: set(),
    ProjectState.FAILED: set(),
}


def is_legal_transition(frm: str, to: str) -> bool:
    return to in LEGAL_TRANSITIONS.get(frm, set())


# ----------------------------------------------------------------- models
@dataclass
class AcceptanceCriterion:
    id: str
    description: str


@dataclass
class TaskSpec:
    task_id: str
    project_id: str
    objective: str
    allowed_paths: List[str]
    forbidden_paths: List[str]
    acceptance_criteria: List[AcceptanceCriterion]
    gate_commands: List[str]
    worker_session_id: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    required_evidence: List[str] = field(default_factory=list)
    worker_harness: str = "codex"
    budgets: Dict = field(default_factory=lambda: {
        "max_local_fixes": 2, "max_replans": 1,
        "max_same_alerts": 1, "max_runtime_seconds": 1800})
    subtask_of: Optional[str] = None     # parent mission_id when part of one

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "TaskSpec":
        acs = [AcceptanceCriterion(**a) for a in d.get("acceptance_criteria", [])]
        return cls(
            task_id=d["task_id"], project_id=d["project_id"],
            objective=d["objective"],
            allowed_paths=list(d.get("allowed_paths", [])),
            forbidden_paths=list(d.get("forbidden_paths", [])),
            acceptance_criteria=acs,
            gate_commands=list(d.get("gate_commands", [])),
            worker_session_id=d.get("worker_session_id"),
            dependencies=list(d.get("dependencies", [])),
            required_evidence=list(d.get("required_evidence", [])),
            worker_harness=d.get("worker_harness", "codex"),
            budgets=dict(d.get("budgets", {})),
            subtask_of=d.get("subtask_of"))

    def validate(self) -> Tuple[bool, str]:
        return _validate(self.to_dict(), "task-spec")


@dataclass
class AuditEvidence:
    type: str
    summary: str
    reference: str = ""


@dataclass
class AuditResult:
    audit_id: str
    task_id: str
    decision: str
    evidence: List[AuditEvidence]
    diagnosis: str
    confidence: float
    failed_criteria: List[str] = field(default_factory=list)
    recommended_action: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["evidence"] = [asdict(e) if not isinstance(e, dict) else e
                         for e in self.evidence]
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "AuditResult":
        ev = [e if isinstance(e, dict) else asdict(e)
              for e in d.get("evidence", [])]
        return cls(
            audit_id=d["audit_id"], task_id=d["task_id"],
            decision=d["decision"], evidence=[AuditEvidence(**e) for e in ev],
            diagnosis=d.get("diagnosis", ""), confidence=d.get("confidence", 0.0),
            failed_criteria=list(d.get("failed_criteria", [])),
            recommended_action=d.get("recommended_action", ""))

    def validate(self) -> Tuple[bool, str]:
        return _validate(self.to_dict(), "audit-result")


@dataclass
class PlannerAction:
    action_id: str
    task_id: str
    action: str
    reason: str
    target_session_id: Optional[str] = None
    message: str = ""
    replacement_task_spec: Optional[Dict] = None
    plan: str = ""   # the Planner's strategy/decomposition (leader capability)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "PlannerAction":
        return cls(
            action_id=d["action_id"], task_id=d["task_id"], action=d["action"],
            reason=d.get("reason", ""),
            target_session_id=d.get("target_session_id"),
            message=d.get("message", ""),
            replacement_task_spec=d.get("replacement_task_spec"),
            plan=d.get("plan", ""))

    def validate(self) -> Tuple[bool, str]:
        return _validate(self.to_dict(), "planner-action")


# --------------------------------------------------------------- verifier
@dataclass
class AcCheck:
    """One acceptance-criterion verdict from the Verifier."""
    ac_id: str
    verdict: str            # PASS | FAIL | UNVERIFIABLE
    note: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "AcCheck":
        return cls(ac_id=d["ac_id"], verdict=d.get("verdict", "UNVERIFIABLE"),
                   note=d.get("note", ""))


@dataclass
class VerifierResult:
    """Independent correctness check — orthogonal to the Auditor.

    The Auditor diagnoses WHAT WENT WRONG from incident evidence; the Verifier
    answers IS THE RESULT ACTUALLY CORRECT: per-AC verdicts against the diff
    and gate output, plus anti-gaming checks (modified tests, self-modified
    ACs, fabricated evidence, gate-output vs claim mismatch).
    """
    verify_id: str
    task_id: str
    verdict: str                                # PASS | FAIL
    ac_checks: List[AcCheck] = field(default_factory=list)
    anti_gaming: List[AcCheck] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "VerifierResult":
        return cls(
            verify_id=d["verify_id"], task_id=d["task_id"],
            verdict=d["verdict"],
            ac_checks=[AcCheck.from_dict(a) for a in d.get("ac_checks", [])],
            anti_gaming=[AcCheck.from_dict(a) for a in d.get("anti_gaming", [])],
            summary=d.get("summary", ""))

    def failed_acs(self) -> List[str]:
        return [c.ac_id for c in self.ac_checks if c.verdict == "FAIL"]

    def gaming_flags(self) -> List[str]:
        return [c.ac_id for c in self.anti_gaming if c.verdict == "FAIL"]


def validate_task_spec(d: Dict) -> Tuple[bool, str]:
    return _validate(d, "task-spec")


def validate_audit_result(d: Dict) -> Tuple[bool, str]:
    return _validate(d, "audit-result")


def validate_planner_action(d: Dict) -> Tuple[bool, str]:
    return _validate(d, "planner-action")


def validate_verifier_result(d: Dict) -> Tuple[bool, str]:
    return _validate(d, "verifier-result")


def validate_mission_plan(d: Dict) -> Tuple[bool, str]:
    return _validate(d, "mission-plan")


# --------------------------------------------------------------- mission
def new_mission_max_subtasks(budgets: Optional[Dict]) -> int:
    """Validate the Worker budget for a newly-created mission.

    Persisted MissionPlans intentionally bypass this check: old runtimes may
    already contain more than two subtasks and must remain resumable without
    re-decomposition.
    """
    raw = (budgets or {}).get("max_subtasks", 1)
    if isinstance(raw, bool):
        raise ValueError(
            "budgets.max_subtasks must be 1 or 2 for a new mission")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw not in ("1", "2"):
            raise ValueError(
                "budgets.max_subtasks must be 1 or 2 for a new mission")
        value = int(raw)
    elif isinstance(raw, (int, float)) and raw in (1, 2):
        value = int(raw)
    else:
        raise ValueError(
            "budgets.max_subtasks must be 1 or 2 for a new mission")
    return value


@dataclass
class MissionSpec:
    """One complete user instruction — the unit of 'fire and forget'.

    The leader Planner decomposes this into SubtaskPlans at mission start;
    subtasks dispatch to N parallel workers, merge, and verify — the user is
    only involved again at HUMAN.
    """
    mission_id: str
    project_id: str
    objective: str
    allowed_paths: List[str]
    forbidden_paths: List[str]
    acceptance_criteria: List[AcceptanceCriterion]
    gate_commands: List[str]
    user_instruction: str = ""
    worker_harness: str = "codex"
    budgets: Dict = field(default_factory=lambda: {
        "max_subtasks": 1, "max_total_replans": 3,
        "max_runtime_seconds": 7200})

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "MissionSpec":
        acs = [AcceptanceCriterion(**a) for a in d.get("acceptance_criteria", [])]
        budgets = dict(d.get("budgets", {}))
        budgets.setdefault("max_subtasks", 1)
        return cls(
            mission_id=d["mission_id"], project_id=d["project_id"],
            objective=d["objective"],
            allowed_paths=list(d.get("allowed_paths", [])),
            forbidden_paths=list(d.get("forbidden_paths", [])),
            acceptance_criteria=acs,
            gate_commands=list(d.get("gate_commands", [])),
            user_instruction=d.get("user_instruction", ""),
            worker_harness=d.get("worker_harness", "codex"),
            budgets=budgets)


@dataclass
class SubtaskPlan:
    """One element of the Planner's mission decomposition."""
    subtask_id: str
    objective: str
    allowed_paths: List[str]
    acceptance_criteria: List[AcceptanceCriterion]
    dependencies: List[str] = field(default_factory=list)
    # Isolated-worker gates: subtask worktrees DON'T contain sibling work, so
    # a mission-wide gate would fail on files other subtasks own. Empty means
    # fall back to mission.gate_commands (single-subtask-mission case).
    gate_commands: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "SubtaskPlan":
        acs = [AcceptanceCriterion(**a) for a in d.get("acceptance_criteria", [])]
        return cls(
            subtask_id=d["subtask_id"], objective=d["objective"],
            allowed_paths=list(d.get("allowed_paths", [])),
            acceptance_criteria=acs,
            dependencies=list(d.get("dependencies", [])),
            gate_commands=list(d.get("gate_commands", [])))


@dataclass
class MissionPlan:
    """Planner's decomposition of a MissionSpec: subtasks + strategy."""
    mission_id: str
    subtasks: List[SubtaskPlan]
    strategy: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "MissionPlan":
        return cls(
            mission_id=d["mission_id"],
            subtasks=[SubtaskPlan.from_dict(s) for s in d.get("subtasks", [])],
            strategy=d.get("strategy", ""))
