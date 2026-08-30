"""Failure-set extraction from test-runner output (deterministic).

Used by the mission-level gate to separate PRE-EXISTING (baseline) failures
from mission-caused ones: the baseline failure set is captured on the
pristine integration tree BEFORE any subtask merge lands, then compared with
the post-merge run. Only NEW failures are fatal (review 簇一: without this
separation, a repo with legacy red tests could never reach MISSION_DONE).

Extractors are tolerant: pytest -q prints `____ test_name ____` section
headers; -rf variants print `FAILED <nodeid>` lines; errors print
`ERROR <nodeid>`. Identity = the captured test name / node id string.
"""
from __future__ import annotations

import re
from typing import List

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_SECTION_HDR = re.compile(r"^_{3,}\s*([A-Za-z0-9_\.\:\[\]\-]+)\s*_{3,}\s*$", re.M)


def extract_failure_ids(output: str) -> List[str]:
    """Return a sorted, de-duplicated list of failing test identifiers."""
    ids = set(_FAILED_LINE.findall(output or ""))
    for name in _SECTION_HDR.findall(output or ""):
        # skip decorative headers like ____ FAILURES ____ / ERRORS ____
        if name.upper() in ("FAILURES", "ERRORS", "SUMMARY"):
            continue
        ids.add(name)
    return sorted(ids)
