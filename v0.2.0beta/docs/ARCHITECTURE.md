# CLAO v0.2 Architecture

CLAO（Closed-Loop Agent Orchestrator）是 AO 之上的 Mission 控制层。正式运行入口只有
Panel 和 `run_mission.py`，二者共享同一 runtime 组装与 preflight。

## Authority boundaries

- `MissionController` 是唯一控制平面，负责 Mission 分解、Worker 派发、状态推进、
  Gate、最终验证、预算和恢复。
- `StateStore` 是 CLAO runtime authority，保存 Mission、Task、transition、alert、
  Planner action、Gate 和 verification 证据。
- AO 是 Worker、Session、conversation、activity 与 Session workspace 的外部
  authority。CLAO 通过 `AOAdapter` 读取公开 API，通过 `ActionExecutor` 执行有限的
  spawn/send/kill 写操作。
- `LoopBus`、`StoreBusProjector`、Markdown、JSONL 和 Panel timeline 都是派生投影；
  它们不参与恢复、预算或裁决。

## Runtime components

```text
Panel / run_mission.py
  → shared preflight
  → MissionController
     ├─ Planner (headless Codex CLI)
     ├─ Auditor (headless Codex CLI)
     ├─ Verifier (headless Codex CLI)
     ├─ deterministic Observer
     ├─ Integration Gate
     ├─ AOAdapter / ActionExecutor
     │    └─ AO Codex Worker
     └─ StateStore
          └─ StoreBusProjector → Panel / Markdown / JSONL
```

Planner 是唯一自动规划角色。Auditor 只提交语义审计结果，不直接控制 Worker；
Verifier 在新 Mission 的正常路径中只在 Final Gate 后提交 Mission 终局复核结果，
不直接控制 Worker。正常路径默认只有一个 Worker；只有用户显式选择且任务确实可独立
并行时，Planner 才能安排最多两个 Worker。普通 Task 在确定性证据充分时先运行 Task
Gate；Task Gate PASS 后不调用 Task Verifier。正常 Mission 只在 Final Gate 后调用
一次 Mission Verifier。

Observer 和 Integration Gate 都是确定性程序，不调用模型。Gate 在显式 worktree 中
运行固定命令，并检查前后 HEAD、index、tracked/untracked 内容的 repository
integrity；必要 Git probe 失败时 fail closed。最终 integration 必须从 clean 状态开始，
通过 Final Gate 和 Mission Verifier 后才能成为 `MISSION_DONE`。

## AO Project workspace contract

已验证的 AO Desktop 0.12.9 Git workspace 要求 Project 具有 `origin` remote。显式
`defaultBranch=<branch>` 时，`refs/remotes/origin/<branch>` 必须可解析；auto 模式
要求 `refs/remotes/origin/HEAD` 指向有效 remote branch。`origin` 可以是网络 remote，
也可以是本地 bare repository。CLAO 的 shared preflight 只检查这一事实，不执行
fetch、不添加 remote、不设置 remote HEAD，也不修改 AO Project config。

## Output and source-control safety

运行状态、证据和 integration 输出都位于 `runtime/<mission-id>/`。`MISSION_DONE` 不会
修改目标 repository 的 `main`/`master`，也不会 push `origin`。任何后续主分支交付
都必须由用户显式发起。
