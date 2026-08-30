"""Read-only Auditor.

Takes a prepared EvidenceBundle (TaskSpec + alert + events + diff + test output
+ AC status + history) and returns an AuditResult (PASS/LOCAL_FIX/REPLAN/HUMAN).

Providers:
  FakeAuditorProvider     - deterministic, for unit tests.
  ClaudeCliAuditorProvider- real `claude -p` headless, --disallowedTools "*"
                           (no tools), --json-schema structured, budget cap.

Auditor is READ-ONLY: it never edits files, runs shell, or controls the Worker.
On format failure: one retry; second failure -> decision HUMAN.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .mission_contracts import (AuditResult, AuditEvidence, AuditDecision,
                        validate_audit_result)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _resolve_exe(bin_name: str) -> str:
    """On Windows, npm shims are .cmd files; subprocess without shell=True
    cannot find 'claude' unless we resolve the full path."""
    import os
    import shutil
    if os.name != "nt":
        return bin_name
    if Path(bin_name).exists():
        return bin_name
    import shutil
    resolved = shutil.which(bin_name)
    if resolved:
        return resolved
    # last resort: common npm global dir
    npm_dir = Path.home() / "AppData" / "Roaming" / "npm"
    for cand in (npm_dir / (bin_name + ".cmd"), npm_dir / bin_name):
        if cand.exists():
            return str(cand)
    return bin_name


def _strip_fences(text: str) -> str:
    """Strip markdown code fences some models wrap JSON in."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


@dataclass
class EvidenceBundle:
    task_spec: Dict
    alert: Optional[Dict]
    events: List[Dict] = field(default_factory=list)
    alerts: List[Dict] = field(default_factory=list)   # aggregated incident
    worker_status: Optional[Dict] = None
    git_diff: str = ""
    test_output: str = ""
    satisfied_criteria: List[str] = field(default_factory=list)
    failed_criteria: List[str] = field(default_factory=list)
    history: Dict = field(default_factory=dict)   # local_fixes/replans counts
    audit_type: str = "ALERT"                     # ALERT | COMPLETION
    # multi-worker attribution: WHICH worker / subtask is this audit about
    worker_id: Optional[str] = None
    subtask_id: Optional[str] = None

    def to_prompt_text(self) -> str:
        return json.dumps({
            "task_spec": self.task_spec,
            "audit_type": self.audit_type,
            "worker_id": self.worker_id,
            "subtask_id": self.subtask_id,
            "alert": self.alert,
            "alerts": self.alerts[-20:],
            "events": self.events[-10:],
            "worker_status": self.worker_status,
            "git_diff": self.git_diff[:4000],
            "test_output": self.test_output[:4000],
            "satisfied_criteria": self.satisfied_criteria,
            "failed_criteria": self.failed_criteria,
            "history": self.history,
        }, ensure_ascii=False, indent=2)


class AuditorProvider:
    def audit(self, bundle: EvidenceBundle, audit_id: str) -> AuditResult:
        raise NotImplementedError


class FakeAuditorProvider(AuditorProvider):
    """Deterministic auditor for tests: LOCAL_FIX if any AC failed, else PASS."""
    def audit(self, bundle: EvidenceBundle, audit_id: str) -> AuditResult:
        if bundle.failed_criteria:
            decision = AuditDecision.LOCAL_FIX
            evidence = [AuditEvidence(
                type="test_failure",
                summary="acceptance criteria not satisfied: %s"
                        % ", ".join(bundle.failed_criteria),
                reference="bundle.failed_criteria")]
            diagnosis = "Worker has not satisfied failed criteria; local fix needed."
            recommended = ("Implement missing functionality in allowed paths; "
                           "do not modify tests or forbidden paths.")
            confidence = 0.9
        else:
            decision = AuditDecision.PASS
            evidence = [AuditEvidence(
                type="test_pass", summary="all acceptance criteria satisfied",
                reference="bundle")]
            diagnosis = "All criteria met."
            recommended = ""
            confidence = 0.95
        return AuditResult(
            audit_id=audit_id, task_id=bundle.task_spec.get("task_id", ""),
            decision=decision, evidence=evidence, diagnosis=diagnosis,
            confidence=confidence, failed_criteria=list(bundle.failed_criteria),
            recommended_action=recommended)


class ClaudeCliAuditorProvider(AuditorProvider):
    """Real read-only Claude Code headless auditor.

    claude -p --output-format json --json-schema <audit-result>
           --disallowedTools "*" --max-budget-usd <n>
    All tools disabled => cannot edit files / run shell / call MCP.
    """

    def __init__(self, claude_bin: str = "claude",
                 budget_usd: float = 0.20, timeout: int = 180,
                 system_prompt_path: Optional[str] = None,
                 model: Optional[str] = None):
        # Self-sufficient env: side entrances (reverify scripts, the web
        # panel) never see run_mission.py's process-level setup.
        from .llm_env import ensure_llm_env
        ensure_llm_env()
        self.bin = _resolve_exe(claude_bin)
        self.budget = budget_usd
        self.timeout = timeout
        sp = system_prompt_path or str(PROMPT_DIR / "auditor.md")
        with open(sp, encoding="utf-8") as f:
            self.system_prompt = f.read()
        with open(PROMPT_DIR.parent / "schemas" / "audit-result.schema.json",
                  encoding="utf-8") as f:
            self.schema = json.load(f)
        # The claude CLI reads ANTHROPIC_MODEL from ~/.claude/settings.json env;
        # if that points at a gateway model that 403s or fails structured
        # output, the auditor breaks. Default to GLM-5.2 (verified working on
        # the ark gateway) unless overridden by the ANTHROPIC_MODEL_AUDITOR env
        # var or the ctor argument.
        self.model = model or os.environ.get("ANTHROPIC_MODEL_AUDITOR",
                                             "GLM-5.2")
        # Write the system prompt to a temp file once; --system-prompt-file
        # avoids the Windows ~8k argv cap (the npm shim runs via cmd.exe).
        import tempfile
        self._sp_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        self._sp_file.write(self.system_prompt)
        self._sp_file.flush()
        self._sp_path = self._sp_file.name

    def _env(self) -> Dict[str, str]:
        e = dict(os.environ)
        e["ANTHROPIC_MODEL"] = self.model
        return e

    def _call(self, bundle: EvidenceBundle, audit_id: str,
              use_schema: bool = True) -> Dict:
        prompt = ("Audit this evidence bundle and output ONLY a JSON object "
                  "matching AuditResult schema. audit_id=%s, task_id=%s.\n\n%s"
                  % (audit_id, bundle.task_spec.get("task_id", ""),
                     bundle.to_prompt_text()))
        # The prompt goes via STDIN, never argv: on Windows the npm shim is a
        # .cmd script run through cmd.exe, whose command line is capped at
        # ~8k chars - a large bundle as an argument silently yields empty
        # stdout.
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
        # claude --output-format json wraps the result; try to extract.
        wrapper = json.loads(out)
        if isinstance(wrapper, dict) and wrapper.get("is_error"):
            raise RuntimeError("claude error subtype=%s"
                               % wrapper.get("subtype"))
        if isinstance(wrapper, dict) and "result" in wrapper:
            inner = wrapper["result"]
            if isinstance(inner, str):
                return json.loads(_strip_fences(inner))
            return inner
        return wrapper

    def audit(self, bundle: EvidenceBundle, audit_id: str) -> AuditResult:
        last_err = ""
        # attempt 1: --json-schema structured output; attempt 2: prompt-only
        # JSON (some gateway models never pass schema-validated retries).
        for attempt, use_schema in ((0, True), (1, False)):
            try:
                obj = self._call(bundle, audit_id, use_schema=use_schema)
                obj.setdefault("audit_id", audit_id)
                obj.setdefault("task_id", bundle.task_spec.get("task_id", ""))
                ok, msg = validate_audit_result(obj)
                if ok:
                    return AuditResult.from_dict(obj)
                last_err = "schema: %s" % msg
            except Exception as e:  # noqa
                last_err = "call: %s" % e
            if attempt == 0:
                time.sleep(0.1)
        # two failures -> HUMAN
        return AuditResult(
            audit_id=audit_id, task_id=bundle.task_spec.get("task_id", ""),
            decision=AuditDecision.HUMAN,
            evidence=[AuditEvidence(type="auditor_format_failure",
                                    summary=last_err or "auditor invalid output")],
            diagnosis="Auditor failed to produce valid output twice.",
            confidence=0.0, failed_criteria=list(bundle.failed_criteria))
