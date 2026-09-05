"""Shared, minimal boundary for structured Codex CLI calls."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Union


class CodexCliError(RuntimeError):
    """A Codex CLI invocation did not produce a usable JSON object."""


def run_codex_json(
        prompt: str,
        schema_path: Union[str, Path],
        model: str = "gpt-5.6-sol",
        timeout: float = 180,
        codex_bin: str = "codex",
        cwd: Optional[Union[str, Path]] = None) -> dict:
    """Run one ephemeral, read-only Codex turn and return its JSON object.

    The task prompt is sent only through stdin.  Codex's
    ``--output-last-message`` file is the sole response source; stdout and
    stderr are used only for short diagnostics.
    """
    source_schema_path = Path(schema_path).resolve()
    workdir = str(Path(cwd).resolve()) if cwd is not None else None
    executable = shutil.which(codex_bin) or codex_bin

    def summary(value: object, limit: int = 400) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        # CLI startup warnings precede the actionable terminal error, so keep
        # the tail while still bounding diagnostics and never including input.
        return text[-limit:]

    with tempfile.TemporaryDirectory(prefix="codex-json-") as temp_dir:
        # Codex structured output requires strict object schemas, while the
        # repository's existing local-validation schemas intentionally allow
        # extra fields and optional properties. Derive an ephemeral strict
        # transport schema without modifying those authoritative files; the
        # Provider still runs the existing local validator on the result.
        try:
            transport_schema = json.loads(
                source_schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexCliError("codex schema could not be read: %s"
                                % summary(exc)) from exc

        def make_strict(node: object) -> None:
            if isinstance(node, dict):
                node_type = node.get("type")
                is_object = node_type == "object" or (
                    isinstance(node_type, list) and "object" in node_type)
                if is_object:
                    if "properties" not in node or not isinstance(
                            node["properties"], dict):
                        raise CodexCliError(
                            "object schema must declare properties")
                    properties = node["properties"]
                    node["additionalProperties"] = False
                    node["required"] = list(properties)
                for value in node.values():
                    make_strict(value)
            elif isinstance(node, list):
                for value in node:
                    make_strict(value)

        make_strict(transport_schema)
        transport_schema_path = Path(temp_dir) / source_schema_path.name
        transport_schema_path.write_text(
            json.dumps(transport_schema, ensure_ascii=False), encoding="utf-8")
        output_path = Path(temp_dir) / "last-message.json"
        command = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox", "read-only",
            "--model", model,
            "--output-schema", str(transport_schema_path),
            "--output-last-message", str(output_path),
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=workdir,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexCliError(
                "codex timed out after %ss; stdout=%s stderr=%s"
                % (timeout, summary(exc.stdout), summary(exc.stderr))) from exc
        except OSError as exc:
            raise CodexCliError("codex launch failed: %s" % summary(exc)) from exc

        if completed.returncode != 0:
            raise CodexCliError(
                "codex exited %s; stdout=%s stderr=%s"
                % (completed.returncode, summary(completed.stdout),
                   summary(completed.stderr)))
        if not output_path.exists():
            raise CodexCliError("codex output-last-message file is missing")
        try:
            raw = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexCliError(
                "codex output-last-message could not be read: %s"
                % summary(exc)) from exc
        if not raw:
            raise CodexCliError("codex output-last-message file is empty")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexCliError(
                "codex output-last-message is not valid JSON: %s"
                % summary(exc)) from exc
        if not isinstance(result, dict):
            raise CodexCliError("codex output-last-message is not a JSON object")
        return result
