#!/usr/bin/env python3
"""Compare two AO daemon OpenAPI contracts (yaml) and report compatibility.

Usage:
    python ao-openapi-diff.py baseline.yaml candidate.yaml

Exit code 0 = compatible (no removals); 1 = breaking changes found; 2 = usage error.

CL-AO critical surfaces are checked explicitly:
  routes used by ao_client.py / observer.py / integration_gate.py and the
  fields they depend on (turnId, role, origin, text, clientMessageId,
  status:"unmodified" semantics markers, latestSequence, etc.).
"""

from __future__ import annotations

import re
import sys

CRITICAL_ROUTES = [
    "/api/v1/projects",
    "/api/v1/sessions",
    "/api/v1/sessions/{sessionId}",
    "/api/v1/sessions/{sessionId}/conversation",
    "/api/v1/sessions/{sessionId}/conversation/messages",
    "/api/v1/sessions/{sessionId}/workspace/files",
    "/api/v1/events",
]

CRITICAL_FIELDS = [
    "clientMessageId",
    "turnId",
    "providerTurnId",
    "latestSequence",
    "oldestSequence",
    "hasMoreBefore",
    "isTerminated",
    "projectId",
    "unmodified",
    "truncated",
    "duplicate",
]


def parse(path: str) -> tuple[set[str], set[str], str]:
    txt = open(path, encoding="utf-8").read()
    routes = set(re.findall(r"^  (/[^\s:]+):", txt, re.M))
    opids = set(re.findall(r"operationId: (\S+)", txt))
    return routes, opids, txt


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    base_routes, base_ops, base_txt = parse(sys.argv[1])
    cand_routes, cand_ops, cand_txt = parse(sys.argv[2])

    removed_routes = sorted(base_routes - cand_routes)
    removed_ops = sorted(base_ops - cand_ops)
    added_routes = sorted(cand_routes - base_routes)
    added_ops = sorted(cand_ops - base_ops)

    print(f"baseline : {len(base_routes)} routes, {len(base_ops)} operations")
    print(f"candidate: {len(cand_routes)} routes, {len(cand_ops)} operations")
    print(f"added    : {len(added_routes)} routes, {len(added_ops)} operations (safe)")
    for r in added_routes:
        print(f"  + {r}")

    breaking = False

    if removed_routes or removed_ops:
        breaking = True
        print("\nREMOVED routes (BREAKING):")
        for r in removed_routes:
            print(f"  - {r}")
        print("REMOVED operations (BREAKING):")
        for o in removed_ops:
            print(f"  - {o}")

    missing_critical = [r for r in CRITICAL_ROUTES if r not in cand_routes]
    if missing_critical:
        breaking = True
        print("\nCL-AO critical routes missing in candidate (BREAKING):")
        for r in missing_critical:
            print(f"  ! {r}")

    missing_fields = [f for f in CRITICAL_FIELDS if f not in cand_txt]
    if missing_fields:
        breaking = True
        print("\nCL-AO critical fields missing in candidate (BREAKING):")
        for f in missing_fields:
            print(f"  ! {f}")

    if not breaking:
        print("\nRESULT: COMPATIBLE — no removals, all CL-AO critical surfaces present.")
        print("Additive changes still need a human skim of the full diff.")
        return 0
    print("\nRESULT: BREAKING CHANGES FOUND — do not upgrade until CL-AO adapts.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
