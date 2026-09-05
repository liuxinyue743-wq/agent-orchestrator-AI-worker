"""Independent read-only Verifier — the "is it actually correct?" role.

Orthogonal to the Auditor:
  Auditor : diagnoses WHAT WENT WRONG from incident evidence (alerts, events).
  Verifier: independently checks IS THE RESULT CORRECT — per-AC verdicts
            against the diff + gate output, plus anti-gaming review
            (worker modified tests, self-modified ACs, fabricated evidence,
            gate output inconsistent with claims).

Verifiers are READ-ONLY model agents using the shared ephemeral Codex CLI
boundary. Deterministic findings (path violations, changed-path facts) are
pre-computed by trusted code and injected into the prompt as facts; the model
does semantic review on top of them — never the reverse.

Providers:
  FakeVerifierProvider     - deterministic, for unit tests.
  CodexCliVerifierProvider - production verifier via the shared Codex CLI
                             structured-output boundary.
Transport failures propagate to the Controller's bounded step retry.  A
schema-invalid response is retried once; a second invalid response is a
provider protocol error, never a semantic FAIL verdict.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .codex_cli import CodexCliError, run_codex_json
from .mission_contracts import AcCheck, VerifierResult, validate_verifier_result

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
SCHEMA_DIR = PROMPT_DIR.parent / "schemas"


@dataclass
class VerifierInput:
    """Everything the Verifier sees. Assembled by trusted controller code."""
    task_spec: Dict
    diff: str = ""                     # git diff vs frozen base (trusted)
    gate_output: str = ""              # real command output from the gate
    changed_paths: List[str] = field(default_factory=list)
    deterministic_findings: List[str] = field(default_factory=list)
    # e.g. ["path violation: tests/test_x.py modified",
    #       "tests/ file changed by worker"]  (facts, pre-computed)
    # User directives addressed to the Verifier (panel channel,
    # owner-ruled): shown to the verifier alongside the trusted evidence.
    user_notes: List[str] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        return json.dumps({
            "task_spec": self.task_spec,
            "git_diff": self.diff[:6000],
            "gate_output": self.gate_output[:6000],
            "changed_paths": self.changed_paths,
            "deterministic_findings": self.deterministic_findings,
            "user_notes": self.user_notes[-10:],
        }, ensure_ascii=False, indent=2)


class VerifierProvider:
    def verify(self, inp: VerifierInput, verify_id: str) -> VerifierResult:
        raise NotImplementedError


class FakeVerifierProvider(VerifierProvider):
    """Deterministic verifier for tests.

    PASS unless: any deterministic finding mentions a violation/tests change,
    or the gate output shows a failing summary line, or the diff contains no
    change at all (nothing to verify). Returns per-AC checks derived from the
    task spec AC ids.
    """
    def verify(self, inp: VerifierInput, verify_id: str) -> VerifierResult:
        acs = [a.get("id", "AC?") for a in
               (inp.task_spec.get("acceptance_criteria") or [])]
        red_flags = [f for f in inp.deterministic_findings
                     if "violation" in f.lower() or "tests/" in f.lower()
                     or "mutated head" in f.lower()]
        gate_bad = any(s in inp.gate_output for s in
                       (" failed", "FAILED", "ERROR", "Traceback"))
        nothing_done = not inp.changed_paths and not inp.diff.strip()
        verdict = "FAIL" if (red_flags or gate_bad or nothing_done) else "PASS"
        ac_checks = [AcCheck(ac_id=a,
                             verdict=("FAIL" if verdict == "FAIL" else "PASS"))
                     for a in acs]
        anti = []
        if red_flags:
            anti.append(AcCheck(ac_id="anti-gaming", verdict="FAIL",
                                note="; ".join(red_flags)))
        return VerifierResult(
            verify_id=verify_id,
            task_id=inp.task_spec.get("task_id", ""),
            verdict=verdict, ac_checks=ac_checks, anti_gaming=anti,
            summary="fake verifier: verdict=%s red_flags=%d gate_bad=%s"
                    % (verdict, len(red_flags), gate_bad))


class CodexCliVerifierProvider(VerifierProvider):
    """Production Verifier using ephemeral, read-only Codex CLI calls."""

    def __init__(self, *, codex_bin: str = "codex", timeout: int = 180,
                 model: Optional[str] = None, cwd: Optional[Path] = None,
                 system_prompt_path: Optional[str] = None):
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.model = model or "gpt-5.6-sol"
        self.cwd = Path(cwd) if cwd is not None else PROMPT_DIR.parent
        prompt_path = Path(system_prompt_path) if system_prompt_path \
            else PROMPT_DIR / "verifier.md"
        self.system_prompt = prompt_path.read_text("utf-8")
        self.schema_path = SCHEMA_DIR / "verifier-result.schema.json"

    def _call(self, inp: VerifierInput, verify_id: str) -> Dict:
        task_input = json.dumps({
            "verify_id": verify_id,
            "task_id": inp.task_spec.get("task_id", ""),
            "verifier_input": json.loads(inp.to_prompt_text()),
            "instruction": ("Output ONLY a VerifierResult JSON object "
                            "matching the schema with the supplied verify_id "
                            "and task_id."),
        }, ensure_ascii=False, indent=2)
        prompt = "%s\n\n# VerifierInput\n%s" % (
            self.system_prompt, task_input)
        return run_codex_json(
            prompt=prompt,
            schema_path=self.schema_path,
            model=self.model,
            timeout=self.timeout,
            codex_bin=self.codex_bin,
            cwd=self.cwd,
        )

    def verify(self, inp: VerifierInput, verify_id: str) -> VerifierResult:
        last_err = ""
        for attempt in range(2):
            # Transport/runtime failures escape immediately so the existing
            # VERIFIER_PENDING step boundary can retry them on the next tick.
            # Only a complete JSON object that fails local validation gets
            # this one in-provider protocol retry.
            obj = self._call(inp, verify_id)
            obj.setdefault("verify_id", verify_id)
            obj.setdefault("task_id", inp.task_spec.get("task_id", ""))
            self._coerce(obj)
            ok, msg = validate_verifier_result(obj)
            if ok:
                return VerifierResult.from_dict(obj)
            last_err = "schema: %s" % msg
            if attempt == 0:
                time.sleep(0.1)
        raise CodexCliError(
            "verifier returned schema-invalid output twice: %s"
            % (last_err or "unknown validation failure"))

    @staticmethod
    def _coerce(obj: Dict) -> None:
        """Normalize documented scalar quirks, not malformed evidence.

        Required arrays remain missing/invalid until full Schema validation
        rejects them. Turning a string or dict into [] would discard evidence
        and could let an invalid PASS response through.
        """
        for k in ("verify_id", "task_id", "summary"):
            if obj.get(k) is None:
                obj[k] = ""
        for lst in ("ac_checks", "anti_gaming"):
            if not isinstance(obj.get(lst), list):
                continue
            for c in obj[lst]:
                if not isinstance(c, dict):
                    continue
                if c.get("note") is None:
                    c["note"] = ""
                if c.get("verdict") not in ("PASS", "FAIL", "UNVERIFIABLE"):
                    c["verdict"] = "UNVERIFIABLE"


# Compatibility for legacy CLI modules outside this migration's edit scope.
# There is only one production Verifier implementation.
ClaudeCliVerifierProvider = CodexCliVerifierProvider
