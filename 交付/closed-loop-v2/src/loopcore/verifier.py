"""Independent read-only Verifier — the "is it actually correct?" role.

Orthogonal to the Auditor:
  Auditor : diagnoses WHAT WENT WRONG from incident evidence (alerts, events).
  Verifier: independently checks IS THE RESULT CORRECT — per-AC verdicts
            against the diff + gate output, plus anti-gaming review
            (worker modified tests, self-modified ACs, fabricated evidence,
            gate output inconsistent with claims).

Verifiers are READ-ONLY model agents: `claude -p` headless with ALL tools
disabled. Deterministic findings (path violations, changed-path facts) are
pre-computed by trusted code and injected into the prompt as facts; the model
does semantic review on top of them — never the reverse.

Providers:
  FakeVerifierProvider      - deterministic, for unit tests.
  ClaudeCliVerifierProvider - real `claude -p` headless, GLM-5.2 by default
                              (ANTHROPIC_MODEL_VERIFIER overrides).
On format failure: one retry; second failure -> verdict FAIL with a format
note (the loop escalates; a verifier that cannot speak cannot approve).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .mission_contracts import AcCheck, VerifierResult, validate_verifier_result

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Reuse the auditor's Windows exe-resolution / fence-stripping helpers.
from .auditor import _resolve_exe, _strip_fences  # noqa: E402


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


class ClaudeCliVerifierProvider(VerifierProvider):
    """Real read-only verifier via headless claude CLI.

    Same invocation discipline as the auditor: prompt via STDIN (Windows ~8k
    argv cap), system prompt via --system-prompt-file, all tools disabled,
    budget cap, two attempts (schema-structured, then prompt-only).
    """

    def __init__(self, claude_bin: str = "claude",
                 budget_usd: float = 0.20, timeout: int = 180,
                 system_prompt_path: Optional[str] = None,
                 model: Optional[str] = None):
        from .llm_env import ensure_llm_env
        ensure_llm_env()
        self.bin = _resolve_exe(claude_bin)
        self.budget = budget_usd
        self.timeout = timeout
        sp = system_prompt_path or str(PROMPT_DIR / "verifier.md")
        with open(sp, encoding="utf-8") as f:
            self.system_prompt = f.read()
        with open(PROMPT_DIR.parent / "schemas" /
                  "verifier-result.schema.json", encoding="utf-8") as f:
            self.schema = json.load(f)
        self.model = model or os.environ.get("ANTHROPIC_MODEL_VERIFIER",
                                             "GLM-5.2")
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

    def _call(self, inp: VerifierInput, verify_id: str,
              use_schema: bool = True) -> Dict:
        prompt = ("Independently verify this result and output ONLY a JSON "
                  "object matching VerifierResult schema. verify_id=%s, "
                  "task_id=%s.\n\n%s"
                  % (verify_id, inp.task_spec.get("task_id", ""),
                     inp.to_prompt_text()))
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
                return json.loads(_strip_fences(inner))
            return inner
        return wrapper

    def verify(self, inp: VerifierInput, verify_id: str) -> VerifierResult:
        last_err = ""
        for attempt, use_schema in ((0, True), (1, False)):
            try:
                obj = self._call(inp, verify_id, use_schema=use_schema)
                obj.setdefault("verify_id", verify_id)
                obj.setdefault("task_id", inp.task_spec.get("task_id", ""))
                self._coerce(obj)
                ok, msg = validate_verifier_result(obj)
                if ok:
                    return VerifierResult.from_dict(obj)
                last_err = "schema: %s" % msg
            except Exception as e:  # noqa
                last_err = "call: %s" % e
            if attempt == 0:
                time.sleep(0.1)
        # A verifier that cannot produce a valid verdict must NOT approve.
        return VerifierResult(
            verify_id=verify_id, task_id=inp.task_spec.get("task_id", ""),
            verdict="FAIL",
            ac_checks=[], anti_gaming=[],
            summary="verifier invalid output: %s" % (last_err or "unknown"))

    @staticmethod
    def _coerce(obj: Dict) -> None:
        """Normalize real-model output quirks before schema validation
        (None -> "" for strings, missing list fields -> [])."""
        if not isinstance(obj.get("ac_checks"), list):
            obj["ac_checks"] = []
        if not isinstance(obj.get("anti_gaming"), list):
            obj["anti_gaming"] = []
        for k in ("verify_id", "task_id", "summary"):
            if obj.get(k) is None:
                obj[k] = ""
        for lst in ("ac_checks", "anti_gaming"):
            for c in obj[lst]:
                if not isinstance(c, dict):
                    continue
                if c.get("note") is None:
                    c["note"] = ""
                if c.get("verdict") not in ("PASS", "FAIL", "UNVERIFIABLE"):
                    c["verdict"] = "UNVERIFIABLE"
