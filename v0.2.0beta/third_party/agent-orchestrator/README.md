# Agent Orchestrator external dependency

CL_AO uses the separately installed AO 0.12.9 baseline. This patch does not
install, upgrade, start, or reconfigure AO. The official installer can be
published as a separate Release asset, with the license/attribution preserved.
Do not add it to Git history or copy developer ao-data/Electron profiles.

Expected filename: `agent-orchestrator-win32-x64-v0.12.9.exe`
Expected size: 130460533 bytes
SHA-256: `1584036ba62a0307063cb0ba03caa1745a6d5bb8fd2e449bcdcf789776d6b037`
Upstream: https://github.com/Untrivial-ai/agent-orchestrator/releases/tag/v0.12.9

These are the prior package's pinned dependency facts, not a new compatibility
claim. No AO installer is needed when the existing AO baseline already works.
