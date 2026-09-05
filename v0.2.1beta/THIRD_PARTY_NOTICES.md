# Third-party notices

## Agent Orchestrator

CL_AO integrates with **Agent Orchestrator (AO)** as an external execution and worktree-management layer.

- Upstream: `Untrivial-ai/agent-orchestrator`
- Tested version: `v0.12.9`
- Copyright: 2026 Untrivial and contributors
- License: Apache License, Version 2.0
- License copy: `third_party/agent-orchestrator/LICENSE-APACHE-2.0.txt`

The AO installer used by the optional packaging workflow is downloaded unchanged from the upstream GitHub Release and is not covered by CL_AO's MIT License. The release bundle must retain the upstream license and all notices already embedded in the AO distribution.

CL_AO is not affiliated with or endorsed by the AO maintainers.

## Coding-agent CLIs and model services

Claude Code, Codex, Kimi and any configured model/provider are external products. They are not included in the source checkout, and their use is governed by their respective licenses, terms, authentication and billing arrangements.

## Provenance of CL_AO licensing text

The CL_AO MIT license text is retained unchanged from the earlier team-provided
`CL_AO-v0.2.0-beta-source-R2.zip`. This patch does not claim ownership of AO or
change licenses of third-party software. The team must confirm contributor
consent before public redistribution. External dependencies are not embedded
in this source/product patch.

Python runtime dependencies: PyYAML, jsonschema and pytest are installed from
requirements.txt into the local virtual environment, not copied from a developer
machine. Their respective upstream license notices remain applicable.
