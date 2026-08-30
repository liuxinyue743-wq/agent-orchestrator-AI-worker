# V0.1 闭环任务 — 基线审计（Phase 0 前置）

记录时间：2026-08-27。所有结论均来自实际命令输出（适配 E 盘当前路径）。

## 路径适配说明

任务书原文使用 `D:\AI-Worker\...`，但当前本机状态为：全部内容已于上一轮迁移至
`E:\智理杯智能体大赛\`，`D:\AI-Worker` 已删除。经负责人确认：**全部路径适配
E 盘**，等价替换如下：

| 任务书路径 | 实际路径 |
|---|---|
| `D:\AI-Worker\ao-supervision-sidecar` | `E:\智理杯智能体大赛\ao-supervision-sidecar` |
| `D:\AI-Worker\ao-app` | `E:\智理杯智能体大赛\ao-app` |
| `D:\AI-Worker\ao-data` | `E:\智理杯智能体大赛\ao-data` |
| `D:\AI-Worker\ao-smoke-test` | `E:\智理杯智能体大赛\ao-smoke-test` |
| `D:\AI-Worker\closed-loop-demo`（待建） | `E:\智理杯智能体大赛\closed-loop-demo` |

环境变量：
- `AO_DATA_DIR = E:\智理杯智能体大赛\ao-data`
- `AO_RUN_FILE = E:\智理杯智能体大赛\ao-data\ao.run`

## 1. Git 基线

`git status`：**sidecar 原先不是 git 仓库**（`fatal: not a git repository`）。
本任务要求每阶段 commit，因此 Phase 0 先执行 `git init` 并建立初始 commit。
- 初始分支：`master`
- 无历史 commit

## 2. 测试基线

`python -m pytest tests -q`：
```
27 passed in 0.06s
```
- 27 个现有测试全部通过；不为让基线通过删除任何测试。

## 3. AO 实际版本与状态

```
$ ao version        -> dev
$ ao status         -> AO daemon: ready  pid=29616  port=61562
```
- 安装包版本以文件名为准：v0.12.7（`agent-orchestrator-win32-x64.exe`，
  SHA-256 已在 TASK-AO-02 校验）。
- daemon 监听 `127.0.0.1:61562`（注意：端口非固定 3001，每次启动由 daemon 选；
  客户端走 `ao.run` 文件读取，不硬编码端口）。

## 4. AO 项目与会话

```
$ ao project ls
ao-smoke-test  smoke-test  single_repo  ao-smoke-tes  ok
scratch        Scratch    scratch      scratch       ok

$ ao session ls          -> (no active sessions)   2 terminated hidden
$ ao orchestrator ls     -> (no orchestrators)
```
- 现有项目 `ao-smoke-test`（路径已重新注册到 E 盘）。
- 无活动 worker、无 orchestrator session。闭环 Demo 需新建独立项目
  `closed-loop-demo`（不复用 smoke-test，避免污染）。

## 5. Agent 就绪状态

```
$ ao agent ls
claude-code  Claude Code  installed   authorized
codex        Codex        installed   authorized
（其余 20+ 均 needs install / auth unknown）
```
- Codex 0.150.1（`codex --version`）→ Worker 用。
- Claude Code 2.1.211（`claude --version`）→ Auditor / Planner 用。
- 任务书禁止接入 GLM/Kimi/OpenCode 等新 Provider，仅用上述两个。

## 6. AO CLI 实际支持的 spawn / send / orchestrator 参数

### `ao spawn` 关键参数（实测）
```
--kind string        Session role: worker or orchestrator (default: worker)
--harness string     claude-code, codex, aider, opencode, grok, ... (含 kimi/muse 等)
--project string     Project id
--name string        Display name (required, max 20 chars)
--mode string        chat | tui
--model string       Agent model override (e.g. gpt-5.6-sol)
--prompt string      Initial prompt
--branch string      Branch (default ao/<session-id>/root)
--skip-agent-check    Skip agent catalog preflight
```
→ Planner 用 `--kind orchestrator --harness claude-code --project <id>`；
Worker 用 `--kind worker --harness codex`。

### `ao send`（实测）
```
--session string   Session id (required)
--message string  Message body (required)
```
→ ActionExecutor 的 `SEND_LOCAL_FIX` 用 `ao send --session <worker> --message <planner_msg>`。

### `ao orchestrator ls`
存在该子命令，当前返回 `(no orchestrators)`。
→ Planner 复用/创建 orchestrator session 可行（需 `ao spawn --kind orchestrator`）。

## 7. Claude CLI Auditor 安全参数（实测，任务 7.2 关键）

`claude --help` 确认本机 2.1.211 支持以下参数：
```
-p, --print                       非交互打印模式（exit 后退出）
--output-format <format>          支持 json
--json-schema <schema>           结构化输出 JSON Schema
--disallowedTools, --disallowed-tools <tools...>   禁用工具
--allowedTools, --allowed-tools <tools...>         允许工具（白名单）
--max-budget-usd <amount>          单次预算上限
--permission-mode <mode>          bypassPermissions | manual | ...
```
→ Auditor 真实调用方案：`claude -p --output-format json --json-schema <audit-result>
--disallowedTools "*" --max-budget-usd <n>`。`--disallowedTools "*"` 即关闭所有工具
（只读、无文件/shell/MCP）。**不使用** `--dangerously-skip-permissions`。

## 8. 当前代码结构与已实现能力

```
src/ao_adapter.py          AO REST/SSE 只读适配 + 非法 JSON 修复
src/event_normalizer.py    AO 原始 -> NormalizedEvent
src/fingerprints.py         错误指纹归一化
src/observer.py            REPEATED_ERROR / NO_PROGRESS 确定性规则
src/models.py              NormalizedEvent / Alert
src/cli.py                 --once / --watch / --fresh / --use-sse
schemas/event.schema.json
schemas/alert.schema.json
config/default.yaml        阈值（不硬编码）
tests/                     27 个测试（指纹/重复错误/无进展/正常/归一化）
```

已实现能力（TASK-AO-02 交付）：
- 只读事件采集（REST + SSE）→ runtime/events.jsonl
- 确定性告警 REPEATED_ERROR / NO_PROGRESS → runtime/alerts.jsonl
- `--once` / `--watch` / `--fresh`

## 9. 当前缺失能力（V0.1 待补）

1. Observer 缺陷：
   - `--watch --fresh` 每次轮询都清空 JSONL（应只首次清空一次）
   - 重启后重复处理历史事件（无持久化 event_id/alert_id 去重）
   - 任意 file_changed 一律 progress=True（应区分 weak/strong progress）
   - 轮询与 SSE 并发无锁保护
2. 无 TaskSpec / AuditResult / PlannerAction / ProjectState 协议与校验
3. 无状态机（合法迁移校验）
4. 无持久状态存储（SQLite closed_loop.db）
5. 无只读 Auditor（ClaudeCliAuditorProvider）
6. 无 AO Planner 适配（orchestrator session + ao send 反馈）
7. 无 ActionExecutor（固定动作映射 + 幂等 + 预算）
8. 无闭环控制器（closed_loop / closed_loop_cli）
9. 无 Integration Gate（worktree 内独立重跑测试）
10. 无可重复 Demo 仓库与脚本
11. 无一键 Start/Stop/Run 脚本
12. 无 V0.1 架构/证据/限制/运维文档

## 10. 基线结论

- 基线测试通过（27/27），不视为缺陷。
- AO daemon 与 Codex/Claude Code 均就绪，可进入闭环联调。
- sidecar 非 git 仓库，Phase 0 先 `git init`。
- 路径已适配 E 盘（负责人确认）。
- 无现有缺陷阻塞 Phase 0 启动。
