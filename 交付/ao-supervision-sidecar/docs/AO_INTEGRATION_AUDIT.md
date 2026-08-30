# AO 集成审计（Phase 1）— Agent Orchestrator v0.12.7 只读接口

- 审计日期：2026-08-27
- 审计人：claude-code（监督侧车 agent）
- AO 版本：安装包 `agent-orchestrator-win32-x64.exe`（v0.12.7，SHA-256
  `e99a81b72ae53a7d36909dc06d3b29d27300fec5bd2f6ea06c2b99ac98156754`，
  130,414,579 字节）；`ao.exe version` 输出 `dev`
- 运行方式：Go daemon 无头运行，`ao-app/resources/daemon/ao.exe daemon`
- 环境变量：`AO_DATA_DIR=E:\智理杯智能体大赛\ao-data`，
  `AO_RUN_FILE=E:\智理杯智能体大赛\ao-data\ao.run`（pid/端口记录在 run 文件）
- 结论：**只读监督可行**。REST + SSE + CLI 三条通道均可用，无鉴权，数据足以支撑
  REPEATED_ERROR / NO_PROGRESS 两类确定性检测。本审计仅记录实际验证过的内容；
  未验证或失败的条目在各自小节如实标注。

---

## 1. Daemon 与进程

| 项 | 实测值 |
|---|---|
| 监听地址 | `127.0.0.1:3001`（仅回环，未绑定外网） |
| 鉴权 | **无**。所有列出的 REST/SSE 端点均未要求任何 header/token（实测） |
| 进程 | 单进程 `ao.exe daemon`；状态文件 `ao-data/ao.run` |
| 数据目录 | `ao-data/data/`：`ao.db`（SQLite）+ `worktrees/<project>/<session>/` |
| 版本命令 | `ao.exe version` → `dev`（CLI 不输出语义版本号；安装包版本以文件名/哈希为准） |

---

## 2. REST 端点（全部实测通过）

Base URL：`http://127.0.0.1:3001`

### 2.1 `GET /api/v1/projects` ✅
```json
{"projects":[{"id":"ao-smoke-test","name":"smoke-test",
  "path":"E:\\智理杯智能体大赛\\ao-smoke-test","kind":"single_repo",
  "sessionPrefix":"ao-smoke-tes","orchestratorAgent":"claude-code"},
  {"id":"scratch","name":"Scratch",
   "path":"E:\\智理杯智能体大赛\\ao-data\\data\\scratch\\default",...}]}
```
监督用法：`get_projects()` → 项目 ID 列表（worker 过滤用 `projectId` 字段匹配）。

### 2.2 `GET /api/v1/projects/{id}` ✅（有编码缺陷，见 §5.2）
```json
{"status":"ok","project":{"id":"ao-smoke-test","name":"smoke-test",
  "kind":"single_repo","path":"E:\\<mojibake>\\ao-smoke-test",
  "repo":"E:\\<mojibake>\\ao-smoke-test-origin.git",
  "defaultBranch":"master","agent":"claude-code",
  "config":{"agentConfig":{},"worker":{"agent":"claude-code",...},
    "orchestrator":{"agent":"claude-code",...},...}}}
```
- ⚠️ 该端点的 `path`/`repo` 中文路径返回 **GBK 乱码**（列表端点 2.1 正常）。
  监督侧不要依赖此端点的 path；需要路径时用 2.1。

### 2.3 `GET /api/v1/sessions` ✅
```json
{"sessions":[{"id":"ao-smoke-test-1","projectId":"ao-smoke-test",
  "kind":"worker","harness":"claude-code","mode":"chat","status":"idle",
  "isTerminated":false,"branch":"ao/ao-smoke-test-1/root",
  "activity":{"state":"idle","lastActivityAt":"2026-08-27T04:31:55.227036Z"}}]}
```
- `status ∈ {idle, terminated, ...}`；`activity.state ∈ {idle, exited, ...}`
- 监督用法：`get_workers(project_id)` = 过滤 `projectId`；每个 worker 的
  `activity.lastActivityAt`（ISO8601 UTC，微秒精度）是时间窗口判定的核心字段。

### 2.4 `GET /api/v1/sessions/{id}` ✅
```json
{"session":{"id":"ao-smoke-test-3","projectId":"ao-smoke-test",
  "kind":"worker","harness":"codex","displayName":"smoke-codex-02",
  "activity":{"state":"idle","lastActivityAt":"2026-08-27T05:24:37.4894206Z"},
  "isTerminated":false,"createdAt":"2026-08-27T05:22:14.546655Z",
  "updatedAt":"2026-08-27T05:24:37.4894206Z","status":"idle"}}
```
- 监督用法：`get_worker_status(worker_id)`。

### 2.5 `GET /api/v1/sessions/{id}/conversation` ✅（监督主数据源）
```json
{"conversationId":"91172f83-...","sessionId":"ao-smoke-test-3",
 "latestSequence":13,"oldestSequence":1,"hasMoreBefore":false,
 "activeBranchId":"91172f83-...:root",
 "turns":[{"id":"3dc2bfb1-...","state":"completed",
   "requestedAt":"2026-08-27T05:22:18Z","completedAt":"2026-08-27T05:24:37Z",
   "diff":{"files":[{"path":"app.py","additions":6,"deletions":2,"status":"modified"}]}}],
 "activities":[ ...见§3... ],
 "messages":[{"kind":"message","id":"...","turnId":"...","sequence":1,
   "role":"user","origin":"human","text":"...","createdAt":"2026-..."}],
 "harness":"codex","mode":"chat","controller":...}
```
- `messages` 全部为 `kind:"message"`（user/assistant 文本）；工具调用、错误、文件
  修改等**结构化事件在 `activities[]`，不在 messages**。
- `turns[].diff.files[]`：`{path, additions, deletions, status}` —— 修改性进度信号。

### 2.6 `GET /api/v1/agents` ✅
```json
{"supported":[{"id":"codex","label":"Codex","authStatus":"authorized",
  "usageCount":2,"lastUsedAt":"2026-08-27T05:22:14.546655Z"},
  {"id":"claude-code","label":"Claude Code","authStatus":"authorized",
   "usageCount":1,"lastUsedAt":"2026-08-27T02:46:13.182373Z"},
  {"id":"agy",...},{"id":"aider",...},...]}
```
- Codex / Claude Code 均 `authStatus:"authorized"`（本机验证）。

### 2.7 `GET /api/v1/notifications` ✅
```json
{"notifications":[{"id":"ntf_...","sessionId":"ao-smoke-test-1",
  "projectId":"ao-smoke-test","type":"needs_input",
  "title":"smoke-claude-01 needs your input",
  "body":"Your agent is waiting on you to continue.",
  "status":"unread","createdAt":"2026-08-27T02:46:22.711988Z",
  "resolvedAt":"2026-08-27T04:16:40.6794304Z",
  "target":{"kind":"session","sessionId":"ao-smoke-test-1"}}],
 "unreadCount":1,"unresolvedCount":0}
```
- `type:"needs_input"` = worker 等待人工审批（Claude Code 权限请求）。

### 2.8 FAILED 条目（如实记录）

| 尝试 | 结果 | 复现 |
|---|---|---|
| `GET /api/v1/sessions/ao-smoke-test-2/conversation`（已终止会话） | **HTTP 409 Conflict** | 会话 `isTerminated=true` 后 conversation 端点拒绝访问 |
| SSE `?since=<seq>` 查询参数增量拉取 | **被忽略**，始终全量 replay | `GET /api/v1/events?since=100` 与无参响应相同 |
| 会话按项目过滤的 SSE 子流（如 `/events?projectId=...`） | **不存在** | 尝试返回全量流 |
| WebSocket 通道 | **未发现**（无 `ws://` 升级端点；产品未暴露） | 未使用 |

---

## 3. `activities[]` —— worker 活动流（监督核心）

`GET /api/v1/sessions/{id}/conversation` 的 `activities[]` 实测字段：
```json
{"kind":"activity","id":"...","turnId":"...","sequence":8,"revision":11,
 "activityKind":"command","status":"completed",
 "summary":"\"...powershell.exe\" -Command 'Get-Location; git branch ...'",
 "detail":{"command":"...","input":...,"error":...},"providerItemId":"..."}
```
实测 `activityKind × status` 分布（3 个真实会话）：
- `reasoning × completed`（思考过程）
- `command × completed`（shell 命令执行）
- `mcp_tool × completed / failed`（工具调用；`detail.input.file_path` 可提取被读文件）
- `file_change × completed / failed`（文件修改事件 → **progress 信号**）
- `approval × resolved`（人工审批）
- `error × failed`（错误事件 → **REPEATED_ERROR 信号**，`summary` 含 provider 错误文本）

**关键限制（影响设计）**：
1. `activities[]` **没有独立时间戳**。时间归属只能靠：
   - `turns[].requestedAt / completedAt`（turn 级，ISO8601）
   - `sessions[].activity.lastActivityAt`（worker 级）
   - 因此 adapter 时间归因粒度 = turn；同一 turn 内多次同类错误只能按
     `sequence` 计数，不能按事件时间戳。
2. `activities[].sequence` 是全局递增序号（conversation 级 `latestSequence`），
   可用于增量轮询游标。

---

## 4. SSE `/api/v1/events` ✅

- 全量 replay-all 流；实测 3 个 `session_created` + 788 个 `session_updated`。
- `Last-Event-ID: <seq>` 请求头**可断点续传**（实测有效）；`?since=` 无效。
- 事件带全局 `seq`；payload 形态（实测）：
  - 多数：`{activity:{...},conversationId,id,isTerminated,sessionId}`
  - 少数：仅 `{id}`；部分为全量会话状态。
- 无按项目过滤；`stream_events(project_id)` 需客户端按 payload 中的
  `sessionId`/`projectId` 过滤。
- ⚠️ 空闲时流无心跳数据，连接会长时间静默（需 socket 超时 + 自动重连 +
  Last-Event-ID 续传兜底）。

---

## 5. 响应格式缺陷（adapter 必须容忍）

### 5.1 非法 JSON 转义
部分端点（如 conversation）返回**非法 JSON**：Windows 路径中的孤立反斜杠
（`E:\智理杯...` 未转义）和 JS 风格 `\'` 转义。`json.loads` 直接报
`Invalid \escape`。**修复策略（已实测通过）**：单字符扫描——
`\` + 合法 JSON 转义字符 → 保留；`\'` → 去掉反斜杠；其余孤立 `\` → 补转义为 `\\`。
该逻辑将内置于 `src/ao_adapter.py`。

### 5.2 中文字段编码不一致
- `GET /api/v1/projects`（列表）中文 path 正常；
- `GET /api/v1/projects/{id}`（详情）`path`/`repo` 中文为 GBK 乱码；
- conversation 的 `detail` 内嵌中文（如命令输出）也存在个别乱码。
- 监督侧：路径信息一律取列表端点；乱码不影响事件类型/计数判定。

### 5.3 数值与时间格式
- 时间统一 ISO8601 UTC，但小数秒位数不固定（`05:22:14.546655Z` vs
  `05:22:05.8404262Z`）→ 解析用 `datetime.fromisoformat` 并容忍尾数变化。

---

## 6. CLI（只读命令，实测）

```text
$ ao.exe version
dev
$ ao.exe session ls
ao-smoke-test:
  ao-smoke-test-1  (3h)  [idle]  worker
  ao-smoke-test-3  (2h)  [idle]  worker
1 terminated session hidden. Use --include-terminated to show.
$ ao.exe session get ao-smoke-test-3 --json
{ "session": { "id":"ao-smoke-test-3", ..., "activity":{"state":"idle",...},
  "isTerminated":false, ... } }
```
- CLI 需要 `AO_DATA_DIR`/`AO_RUN_FILE` 指向 daemon 数据目录才能连上。
- 写操作（`session spawn` 等）在 TASK-AO-01 已单独验证；监督侧 **只用只读命令**。

---

## 7. SQLite（只读旁路，可选）

`ao-data/data/ao.db` 可用 Python `sqlite3` 只读打开（`file:...?mode=ro`）。
实测含表：`sessions, conversations, conversation_messages,
conversation_activities, conversation_turns, notifications, projects, ...`。
- 仅用于审计核对，不进 adapter 主路径（REST 已覆盖同样信息）。

---

## 8. 监督需求 → 数据映射（结论）

| 监督需求 | 数据来源（已验证） |
|---|---|
| 项目列表 | `GET /api/v1/projects` |
| 某项目全部 worker | `GET /api/v1/sessions` 过滤 `projectId` |
| worker 状态/最后活跃 | `GET /api/v1/sessions/{id}` 或列表内嵌 |
| 错误事件（指纹来源） | `activities[]` 中 `activityKind=="error"` 的 `summary`/`detail.error`；`command`/`mcp_tool` status=failed（失败的测试运行，实测）与 `file_change` failed 也归一化为 error |
| 活动事件（活动计数） | `activities[]` 中 `command/mcp_tool` 等（`activity` 类型） |
| 进度事件（进展计数） | `activities[]` 中 `activityKind=="file_change" && status=="completed"`；`turns[].diff.files` 非空 |
| 实时事件流 | SSE `/api/v1/events`（全量 replay + Last-Event-ID 续传，客户端过滤） |
| 人工卡住（needs_input） | `GET /api/v1/notifications` 或 SSE 中 `needs_input` 通知 |
| 增量轮询游标 | `conversation.latestSequence` + `activities[].sequence` |

**确定可行**：REPEATED_ERROR（同 project+worker+指纹 ≥3×/10min）取 error
activity 的 summary 做指纹；NO_PROGRESS（15min 内 ≥8 活动事件且 0 进度事件）
取 activity 计数与 file_change/diff 计数，窗口锚点用 `lastActivityAt`。

---

## 9. 稳定性实测

- daemon 跨多小时运行稳定（本审计期间持续在线，无重启）。
- REST 并发/单发均正常；SSE 空闲无心跳（见 §4）。
- conversation 大响应（10KB+）需容忍非法转义（§5.1 修复后解析成功）。
- 终止会话 conversation → 409，adapter 需捕获并跳过。

## 10. 已知不提供（监督侧不能用）

- 无 WebSocket 推送；无按项目/会话过滤的 SSE 子流。
- `activities[]` 无独立时间戳（只能用 turn/session 级时间）。
- 无内建"重复错误次数"或"进展统计"聚合 API（需侧车自行聚合）。
- 无鉴权接口 —— 仅限本机 127.0.0.1，勿暴露端口。
