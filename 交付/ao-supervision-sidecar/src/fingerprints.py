"""Deterministic message fingerprinting for REPEATED_ERROR detection.

Goal: two error messages that are "the same failure" must produce the same
fingerprint even if timestamps, random ids, temp paths, ANSI colors, minor
whitespace or line numbers differ.
"""
from __future__ import annotations

import re
from typing import Dict

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ISO_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})"
)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?(?:ms|us|s|min|h)\b")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")
_UNIX_PATH = re.compile(r"(?<![\w.])(?:/[^\s\"']+)+")
_PATH_SEGMENT_HASH = re.compile(r"(?:^|[\\/])[0-9a-fA-F]{8,}(?:$|[\\/])")
_LINE_NUM = re.compile(r"(\.\w+|:\d+):\d+\b")
_RETRY = re.compile(r"\b\d+/\d+\b")
_WHITESPACE = re.compile(r"\s+")

_DEFAULTS = {
    "strip_ansi": True,
    "normalize_timestamps": True,
    "normalize_ids": True,
    "normalize_paths": True,
    "normalize_line_numbers": True,
    "collapse_whitespace": True,
    "max_length": 200,
}


class Fingerprinter:
    """Normalize an error message into a stable fingerprint string."""

    def __init__(self, options: Dict | None = None):
        self.opts = {**_DEFAULTS, **(options or {})}

    def normalize(self, text: str) -> str:
        if not text:
            return ""
        s = text
        if self.opts["strip_ansi"]:
            s = _ANSI.sub("", s)
        if self.opts["normalize_timestamps"]:
            s = _ISO_TS.sub("<ts>", s)
            s = _DURATION.sub("<dur>", s)
        if self.opts["normalize_ids"]:
            s = _UUID.sub("<id>", s)
            s = _LONG_HEX.sub("<hex>", s)
        if self.opts["normalize_paths"]:
            # Windows paths first (they may contain chars that confuse UNIX_PATH).
            s = _WIN_PATH.sub("<path>", s)
            s = _UNIX_PATH.sub("<path>", s)
            # Hash-like path segments (node_modules/.cache/abc123def) -> <seg>
            s = _PATH_SEGMENT_HASH.sub("<seg>", s)
        if self.opts["normalize_line_numbers"]:
            s = _LINE_NUM.sub(r"\1:<line>", s)
            # Retry counters ("Reconnecting... 2/5") drift between occurrences.
            s = _RETRY.sub("<retry>", s)
        if self.opts["collapse_whitespace"]:
            s = _WHITESPACE.sub(" ", s).strip()
        return s[: self.opts["max_length"]]

    def fingerprint(self, text: str) -> str:
        """Fingerprint of a message; empty string if nothing remains."""
        return self.normalize(text).strip()
