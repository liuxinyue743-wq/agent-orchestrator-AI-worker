# AO v0.12.9 集成面审查

## 1. 审查范围和锁定版本

- 上游仓库：`agent-orchestrator-upstream`，tag `v0.12.9`，commit `4cbb4b6ced1ad93f79641a2347d2342f1ffd218a`。
- 本次只静态审查上游源码，以及上游 `AGENTS.md`、`docs/architecture.md`、`docs/cli/README.md`；没有启动 daemon、修改上游或实现集成代码。
- 关键结论以路由注册、DTO 和 service 调用链为证据。`/api/v1/openapi.yaml` 由 daemon 同源提供；注册入口见 `backend/internal/httpd/api.go` 的 `(*API).Register`，生成契约见 `backend/internal/httpd/apispec/openapi.yaml` 和 `backend/internal/httpd/apispec/specgen/build.go`。以下结论只锁定到 v0.12.9，不推定后续版本兼容性。

## 2. 已验证的架构边界

| 边界 | v0.12.9 中的真实形态 | 外部 Adapter 判断 | 源码证据 |
| --- | --- | --- | --- |
| REST API | daemon 在 `/api/v1` 注册有 OpenAPI 描述的读取和控制路由 | **首选正式入口**；限定本机、固定版本并按 OpenAPI 校验 | `backend/internal/httpd/api.go`：`(*API).Register`；`backend/internal/httpd/controllers/*.go`：各 `Register` |
| SSE | `GET /api/v1/events` 提供带序号、可重放的 CDC 事件；`after` 或 `Last-Event-ID` 是游标 | **适合作为失效通知**；收到事件后重新读取 REST 快照，不把事件负载当完整状态 | `backend/internal/httpd/events.go`：`EventsController.Register`、`stream`、`parseEventsAfter`；`backend/internal/cdc/event.go`：`Event`、`EventType` |
| WebSocket | `GET /mux` 是终端 PTY 多路复用通道 | **不用于监督或派发反馈**；它是终端传输，不是状态或消息控制 API | `backend/internal/httpd/terminal_mux.go`：`mountTerminalMux`；`backend/internal/terminal/manager.go`：终端 attach/stream 实现 |
| `ao` CLI | `project`、`session`、`spawn`、`send`、`orchestrator`、`pr`、`review` 等命令最终调用本地 daemon HTTP | 可用于人工诊断和运行冒烟，**不作为常驻 Adapter 主接口**；部分 CLI DTO 比 REST 少字段，且没有 `ao events` | `backend/internal/cli/root.go`：`NewRootCommand`；`backend/internal/cli/client.go`：`doJSONPathWithHeadersAndTimeout`；`backend/internal/cli/session.go`：`sessionDTO`；`docs/cli/README.md` |
| daemon 发现 | CLI 从 `running.json` 读取 PID/端口并访问 `http://127.0.0.1:<port>` | Adapter 可复用这一发现约定并用健康检查确认实例；不要把 runfile 当业务状态 | `backend/internal/runfile/runfile.go`：`Info`、`Read`、`Write`；`backend/internal/cli/client.go`：`doJSONPathWithHeadersAndTimeout` |
| 内部 Go service | HTTP controller 调用 `backend/internal/service/*`、`session_manager` 和 ports | **不是外部接口**；Go `internal` 包和内部方法不应被 Adapter 导入或复制耦合 | `backend/internal/httpd/controllers/sessions.go`：`SessionService`；`backend/internal/service/session/service.go`；`backend/internal/session_manager/manager.go` |
| SQLite | session、PR、review 等事实及 `change_log` 由内部 store/migration 管理，SSE 以其为 CDC 源 | **不得直接读写作为正式集成**；会绕过派生状态和 service 语义，并耦合内部 schema | `backend/internal/storage/sqlite/store/changelog_store.go`；`backend/internal/storage/sqlite/migrations/0103_review_run_cdc.sql`；`backend/internal/cdc/poller.go` |

daemon 的主 API 是本机进程边界，不是远程服务边界。v0.12.9 的 CLI 客户端显式访问 `127.0.0.1`，上游 `AGENTS.md` 也规定 primary listener 为 loopback 且不加认证；因此 Adapter 应与 AO 同机运行，不能把该端口暴露到不受信网络。

## 3. 能力表

| 所需能力 | 支持 | 入口或路由 | 对应源码文件/函数 | 限制 |
| --- | --- | --- | --- | --- |
| 读取 Project | 是 | `GET /api/v1/projects`；`GET /api/v1/projects/{id}` | `backend/internal/httpd/controllers/projects.go`：`ProjectsController.Register`、`list`、`get`；`backend/internal/service/project/types.go`：`Summary`、`Project` | 当前 CDC 类型没有 project create/update 事件；项目变化需重新轮询读取 |
| 读取 Session / Worker | 是 | `GET /api/v1/sessions?project=...&active=...`；`GET /api/v1/sessions/{sessionId}` | `backend/internal/httpd/controllers/sessions.go`：`Register`、`list`、`get`、`parseSessionListFilter`；`backend/internal/domain/session.go`：`Session` | Worker 与 Orchestrator 都是 session，以 `kind` 区分；CLI 的 `sessionDTO` 不是完整 REST read model |
| 读取 Chat Conversation / Planner 或 Worker 的结构化输出 | 是，仅限 Chat mode | `GET /api/v1/sessions/{sessionId}/conversation?beforeSequence=...&limit=...` | `backend/internal/httpd/controllers/conversations.go`：`ConversationsController.Register`、`snapshot`、`conversationSnapshotResponse`；`backend/internal/service/chat/service.go`：`requireChatSession`、`SnapshotPage`；`backend/internal/domain/conversation.go`：`ConversationTurn`、`ConversationMessage`；OpenAPI：`getSessionConversation`、`ConversationSnapshotResponse` | 快照包含 `turns`、`messages`、`activities`、`latestSequence`、`oldestSequence`、`hasMoreBefore`；公开 `ConversationMessageResponse` 含 `role`、`origin`、`text`、`turnId`，不回显 `clientMessageId`。`beforeSequence` 读取更早序号，`limit` 默认 200、范围 1–500。TUI session 会得到 `SESSION_MODE_MISMATCH` |
| 读取当前 activity 和显示状态 | 是 | 同 session 读取路由；响应含原始 `activity.state` 和派生 `status` / `scmStatus` | `backend/internal/domain/activity.go`：`ActivityState`、`Activity`；`backend/internal/domain/session.go`：`Session`；`backend/internal/service/session/status.go`：`deriveStatus`；`backend/internal/service/session/service.go`：`toSession` | activity 来自 agent hook，不由 transcript 推断；显示状态是读取时派生值，不是持久化事实。`blocked` 表示等待决策，自动化不得注入输入 |
| 读取 branch | 是 | session 响应中的 `branch` | `backend/internal/httpd/controllers/sessions.go`：`SessionView`、`sessionView` | `branch` 是 HTTP view 从内部 metadata 映射出的字段；应依赖 REST 字段而不是 metadata 存储结构 |
| 读取 worktree 内容 | 是 | `GET /api/v1/sessions/{sessionId}/workspace/files`、`workspace/file`、`workspace/file/blob` | `backend/internal/httpd/controllers/sessions.go`：`listWorkspaceFiles`、`getWorkspaceFile`、`getWorkspaceFileBlob` | 适合受控文件/差异读取；不是任意文件系统入口 |
| 读取 worktree 绝对路径 | 有条件 | `GET /api/v1/desktop/sessions/{sessionId}/workspace` | `backend/internal/httpd/controllers/desktop_workspace.go`：`DesktopWorkspaceController.Register`、`location`；`backend/internal/httpd/controllers/dto.go`：`DesktopWorkspaceLocationResponse` | 普通 session 响应刻意不泄露绝对路径；该路由是 loopback-only 的 Desktop handoff。Adapter 默认不应依赖，确需路径时先做运行验证 |
| 读取 PR、CI、代码评审、可合并/已合并状态 | 是 | session 摘要中的 `prs`；详细读取 `GET /api/v1/sessions/{sessionId}/pr`；AO review 运行态为 `GET /api/v1/sessions/{sessionId}/reviews` | `backend/internal/httpd/controllers/sessions.go`：`listPRs`；`backend/internal/httpd/controllers/dto.go`：`SessionPRSummary`；`backend/internal/service/session/pr_summary.go`：`ListPRSummaries`、`summarizeCI`、`summarizeReview`、`summarizeMergeability`；`backend/internal/httpd/controllers/reviews.go`：`list` | SCM 是轮询缓存：默认 PR/CI 30 秒、review 2 分钟，见 `backend/internal/observe/scm/observer.go`；还依赖 provider 凭据。merge 结果由 PR state/summary 读取，不保证即时 |
| 接收状态变化事件 | 部分支持 | SSE `GET /api/v1/events?after=<seq>`，或 `Last-Event-ID` | `backend/internal/httpd/events.go`：`stream`；`backend/internal/cdc/event.go`：`EventType`；`backend/internal/cdc/poller.go`：`Poller` | 持久化 CDC 只有 session、PR/check/review 类型，没有 conversation turn/message 事件；`/events` 不能直接给出 Planner 决策正文。`session_updated` 最多作为重读 conversation snapshot 的唤醒信号，是否足够可靠仍需 M0-4 验证 |
| 接收 worktree 文件变化 | 是，非持久 | SSE `GET /api/v1/sessions/{sessionId}/workspace/events` | `backend/internal/httpd/controllers/sessions.go`：`RegisterStreams`、`streamWorkspaceChanges` | 仅在客户端订阅时监看，事件负载不是完整 diff，也没有 CDC 重放；只能触发重新读取 workspace API |
| 向 Chat-mode Planner / Worker 发送可跟踪消息 | 是，仅限 Chat mode | `POST /api/v1/sessions/{sessionId}/conversation/messages` | `backend/internal/httpd/controllers/conversations.go`：`send`；`backend/internal/httpd/controllers/dto.go`：`SendConversationMessageRequest`、`SendConversationMessageResponse`；`backend/internal/service/chat/service.go`：`Send`；OpenAPI：`sendSessionConversationMessage` | 支持 `ClientMessageID` 幂等键；`202` 响应包含 `TurnID`、`ProviderTurnID`、`State`、`Duplicate`（重复时 turn 字段可为空）。Duplicate 恢复必须读取公开 snapshot，以唯一精确 user/human 正文取得非空 `turnId`；消息接受或恢复不表示 Agent 已处理完成 |
| 通用 Chat/TUI 消息入口 | 是 | `POST /api/v1/sessions/{sessionId}/send`；CLI 为 `ao send` | `backend/internal/httpd/controllers/sessions.go`：`send`；`backend/internal/httpd/controllers/dto.go`：`SendSessionMessageResponse`；`backend/internal/session_manager/manager.go`：`Manager.Send`；`backend/internal/session_manager/chat_spawn.go`：`sendChat`；OpenAPI：`sendSessionMessage` | 按 session 持久化 mode 路由到 Chat 或 TUI，但响应只有 `ok`、`sessionId`、`message`，没有可靠自动闭环所需的结构化 turn 回执。适合作为 TUI 或人工诊断入口；Chat-mode 自动 Adapter 应优先使用 `conversation/messages` + snapshot |
| 创建 Worker / Session | 是 | `POST /api/v1/sessions`；CLI 为 `ao spawn` | `backend/internal/httpd/controllers/sessions.go`：`spawn`；`backend/internal/httpd/controllers/dto.go`：`SpawnSessionRequest`；`backend/internal/service/session/service.go`：`Spawn` | 这是控制接口，但本项目中只能由唯一 Planner 使用；Observer/Auditor/Adapter 不得据此自行扩张 Worker |
| 结束 Worker | 是 | `POST /api/v1/sessions/{sessionId}/kill`；CLI 为 `ao session kill` | `backend/internal/httpd/controllers/sessions.go`：`kill`；`backend/internal/service/session/service.go`：`Kill`；`backend/internal/session_manager/manager.go`：`Manager.Kill` | 同样应只由 Planner 决策。AO 会终止运行时；脏 workspace 的保留/回收语义不能等同于删除任务证据 |
| 继续派发给同一 Worker | 是 | Chat-mode 活跃 session 使用 `conversation/messages`，随后读取 conversation snapshot；TUI 或人工诊断可使用通用 `/send`；已终止 session 另有 `restore` | `backend/internal/httpd/controllers/conversations.go`：`send`、`snapshot`；`backend/internal/httpd/controllers/sessions.go`：`send`、`restore` | `restore` 是生命周期控制，只能由 Planner 决策；Auditor 只返回证据。turn 接受、处理完成和任务验收是三个不同阶段 |
| 读取 Project Orchestrator | 是 | `GET /api/v1/orchestrators`；`GET /api/v1/orchestrators/{id}`，再对 Chat-mode session 读取 conversation snapshot | `backend/internal/httpd/controllers/sessions.go`：`listOrchestrators`、`getOrchestrator`；`backend/internal/domain/conversation.go`：`ConversationScopeProject`；`backend/internal/httpd/controllers/conversations.go`：`snapshot` | API 读取的是 kind 为 orchestrator 的 session，没有单独“Planner 状态机”API；MVP 必须选择 active Chat-mode Orchestrator 才能读取结构化 Planner 输出 |
| 创建/驱动 Orchestrator | 部分支持 | `POST /api/v1/orchestrators`；`POST /api/v1/orchestrators/delegate`；Chat-mode 消息使用 `conversation/messages` | `backend/internal/httpd/controllers/sessions.go`：`spawnOrchestrator`、`delegateTask`；`backend/internal/service/session/service.go`：`SpawnOrchestrator`；`backend/internal/service/session/delegation.go`：`DelegateTask`；`backend/internal/httpd/controllers/conversations.go`：`send` | 没有类型化的“重规划/采纳审计结论”API。`DelegateTask` 会直接创建 Worker，不是回传审计结论的入口，Adapter 不应调用 |
| 把审计结果返回项目级 Planner 并读取决策 | 可以用 Chat Conversation API 组成闭环 | 读取 active Chat-mode Orchestrator；调用 `conversation/messages`；记录 `TurnID`；循环读取 conversation snapshot | `backend/internal/httpd/controllers/conversations.go`：`send`、`snapshot`、`conversationSnapshotResponse`；`backend/internal/httpd/controllers/dto.go`：Conversation request/response DTO；`backend/internal/domain/conversation.go`：turn/message 类型 | AO 没有原生类型化 verdict/decision DTO 或路由；`PASS`、`LOCAL_FIX`、`REPLAN`、`HUMAN` 是本项目未来消息协议。Adapter 只能在读到并解析 Planner 输出后，按 Planner 决策反馈 Worker |

`POST /api/v1/sessions/{sessionId}/activity` 是 agent hook 的信号摄取入口（`SessionsController.activity`、`ActivityRecorder.ApplyActivitySignal`），不是 Adapter 用来伪造状态或驱动 Worker 的控制接口。

## 4. 推荐的最小集成路线

推荐 **外部本机 Adapter 直接调用 AO daemon 的 REST API，并用 SSE 做变化通知**。不需要 AO 内部扩展或 fork；CLI 只保留为人工检查和运行实验工具。daemon 发现、健康检查、OpenAPI 锁定校验以及 Project/Session/SCM 初始快照仍按前述 REST 路径完成。

MVP 增加一项明确模式约束：项目级 Planner / Orchestrator 必须使用 Chat mode，M0-4 的测试 Worker 也使用 Chat mode，以获得结构化输入、turn 跟踪和结构化输出读取能力。TUI 自动闭环不作为 MVP 完成条件；未来只有验证出可靠的 TUI 输出观察路径后才能加入。

最小 Planner 决策闭环如下：

1. 读取 Project 的 active Chat-mode Orchestrator，并确认其 session mode。
2. 调用 `POST /api/v1/sessions/{sessionId}/conversation/messages`，以唯一 `ClientMessageID` 发送结构化 `AuditReport`。
3. 记录 `202 Accepted` 响应中的 `TurnID`、`ProviderTurnID`、`State` 和 `Duplicate`；该响应只证明 turn 被 AO 接受或识别，不证明 Planner 已完成处理。
4. 若 `Duplicate=false`，使用响应中的非空 `TurnID`；若 `Duplicate=true`，读取公开 Conversation Snapshot，只在 `role=user`、`origin=human`、`text` 与本次完整请求严格相等且 `turnId` 非空的消息恰好一条时恢复该 `turnId`。零条、多条、非 human、正文不一致或无 `turnId` 均 fail closed。
5. 订阅 `GET /api/v1/events`，把 `session_updated` 仅作为唤醒提示；收到提示或轮询到期后，重新读取 conversation snapshot，并按 `latestSequence` / `oldestSequence` / `hasMoreBefore` 处理分页。
6. 以已接受或恢复的 `TurnID` 和项目消息协议中的审计标识关联输入与输出，直到读到对应的 Planner 决策，或达到超时、断线恢复失败、无法关联而转人工介入的条件。
7. 解析 `PASS`、`LOCAL_FIX`、`REPLAN`、`HUMAN` 等项目定义的决策。
8. 只有 Planner 作出决定后，Adapter 才对目标 Chat-mode Worker 执行状态门禁，并通过其 `conversation/messages` 发送后续指令；Worker Duplicate 使用完全相同的恢复规则。
9. Worker 的处理结果同样通过 conversation snapshot 与任务契约要求的代码、Git 和测试证据验证，不能以消息接受或 turn 完成替代任务验收。

AO v0.12.9 没有原生类型化的 `PASS`、`LOCAL_FIX`、`REPLAN`、`HUMAN` DTO 或路由；这些值属于本项目未来定义的消息协议。公开 `ConversationMessageResponse` 不含 `clientMessageId`，因此 Duplicate 恢复不能按该字段关联，也不得退化为最近消息、sequence 顺序、模糊文本或 SQLite 查询；唯一精确正文匹配是已验证的公开 API 边界。

选择 REST 而非 CLI 的原因是：CLI 本身仍经 HTTP 调 daemon，却增加子进程和输出解析层；`backend/internal/cli/session.go` 的 DTO 未暴露完整 branch/PR/SCM read model，`backend/internal/cli/orchestrator.go` 只有 list，且上游 `docs/cli/README.md` 明确没有 `ao events`。REST + SSE + conversation snapshot 覆盖了 M0 要验证的结构化读取、消息接受、Planner 决策读取和 Worker 反馈路径；可靠性仍以 M0-4 运行实验为准。

## 5. 仍需通过运行实验验证的事项

以下源码只能证明入口和调用链存在，不能替代 M0-4 的端到端实验：

- 在本机 v0.12.9 安装形态下，`running.json` 的实际位置、daemon 端口发现、健康检查和 OpenAPI 响应。
- `/events` 的首次订阅、断线重放、游标越界和客户端处理变慢时的恢复行为；确认每类关键变化都能通过“事件 + REST 重读”观察到。
- Chat-mode Worker 的 conversation snapshot 是否能稳定读取完整 turn、message 和分页序号。
- Chat-mode Orchestrator 的 conversation snapshot 是否能稳定读取 Planner 输出。
- `TurnID`、`State` 与 Agent 最终处理完成之间的真实关系，尤其是 queued/running 到终态及最终 assistant message 的可见时机。
- 持久化 CDC 没有 conversation 事件时，`session_updated` 是否足以作为 conversation snapshot 重读提示；若不足，确定安全轮询策略。
- 超时、SSE 断线、snapshot 读取失败、无法关联输出或没有有效 Planner 决策时，如何确定性转为 `HUMAN`。
- Chat-mode Worker 在 active、idle、waiting_input、blocked、exited、terminated 以及界面切换期间的消息接受、排队、可观察处理结果和状态门禁。
- 真实 GitHub/GitLab 凭据下 PR、CI、review、merge 数据的完整度、轮询延迟、限流和失效表现。
- 如果 Adapter 确实需要绝对 worktree 路径，验证 Desktop workspace route 在目标 daemon 启动方式下可用；否则坚持使用 workspace 文件 API。

## 6. 明确不应直接依赖的内部实现

- **SQLite 表、migration、trigger 和 `change_log`**：不得直接查询或写入 `backend/internal/storage/sqlite`；正式读取走 REST，变化通知走 SSE。
- **内部 Go service / manager / ports**：不得从外部 Adapter 导入 `backend/internal/service`、`backend/internal/session_manager` 或存储实现；这些路径只用于本次证明 REST 行为。
- **session metadata 的 JSON/数据库形态**：branch 应取 HTTP `SessionView.branch`，worktree 内容应取 workspace API；不要解析内部 metadata 或推导内部目录布局。
- **`/mux` 终端 WebSocket**：不得用模拟终端输入替代正式消息路由；Chat-mode 自动化走 `conversation/messages`，TUI 或人工诊断才考虑通用 `/send`，否则会绕开 mode 路由和 session guard。
- **`POST /api/v1/sessions/{sessionId}/activity`**：不得由监督程序写入伪造 activity；它是 AO hook 摄取接口。
- **`POST /api/v1/orchestrators/delegate`**：不得用它回传审计结果；源码语义包含直接创建 Worker，与本项目“控制权只属于一个 Planner”的约束冲突。
- **Desktop 绝对路径路由作为默认依赖**：该路由虽进入 v0.12.9 REST/OpenAPI，但用途明确限定为本机 Desktop handoff；只有 workspace 文件 API 无法满足已确认需求时才考虑，并先运行验证。

## 7. M0-4 运行验证结果

- 运行环境：2026-08-28 在 Windows 本机运行 AO Desktop v0.12.9；以一次性 PowerShell/HTTP 操作充当临时外部 Adapter，没有修改 AO 或增加产品代码。
- daemon 与契约：按 v0.12.9 的 `running.json` 约定成功发现存活 daemon；live `GET /api/v1/openapi.yaml` 返回成功，且 Project、Session、Orchestrator 和两个目标 Conversation 路由与锁定源码记录一致。
- 目标与快照：当前 Project 唯一匹配一个带就绪标记的 active Chat-mode Orchestrator 和一个不同的 active Chat-mode Worker；二者均未 blocked 或 terminated，Conversation Snapshot 均可读取，Worker 在投递前没有 staged、unstaged、untracked 或 committed 变化。
- `ClientMessageID` 幂等：第一次 Planner 提交返回 `202`，`TurnID` 和 `ProviderTurnID` 非空，`State=running`、`Duplicate=false`；以完全相同的请求体和 `ClientMessageID` 第二次提交仍返回 `202`，turn 字段和 state 为空、`Duplicate=true`，没有产生第二个 provider turn。
- Planner 闭环：Planner 消息被 AO 接受；通过 Conversation Snapshot 读到与本次审计标识关联的合法单行 JSON，决策为 `LOCAL_FIX`，目标 Worker 一致且 instruction 非空。Planner 没有创建、终止或委派其他 Worker，也没有修改项目文件。
- Worker 闭环：仅把 Planner 返回的 instruction 投递给目标 Worker；AO 返回 `202`、非空 turn 标识、`State=running`、`Duplicate=false`。随后从同一 Worker 的 Conversation Snapshot 读到精确 ACK；没有向其他 Worker 投递消息，目标 Worker 没有文件变化、提交或 PR。
- SSE 观察：Planner 消息处理期间观察到与 Planner session 相关的 `session_updated`；Worker 消息处理期间没有观察到目标 Worker 的相关事件。SSE 事件不含 conversation 正文或结构化决策，因此只能作为尽力而为的唤醒信号，不能替代 REST 快照读取。
- 可靠性边界：正式 Adapter 必须保留低频 Conversation 轮询兜底，并在超时、快照失败、输出无法解析或无法关联、目标 blocked 等情况下确定性转为 `HUMAN`；本次以 2 秒间隔、90 秒上限验证的轮询路径可完成闭环。
- 结论：REST + Conversation API 的 Planner → Worker 核心反馈闭环成功；SSE conversation 唤醒不足，不能单独承担闭环进度通知。实验结束时目标会话数量不变，Planner/Worker 工作区干净，Worker 没有新增提交或 PR，`main` 未变化。
- 仍未验证：SSE 断线重连和游标重放、并发或 queued turn、长时运行与超时恢复、blocked/exited 等其他状态门禁、除 `LOCAL_FIX` 外的决策分支、TUI-mode 闭环，以及真实 PR/CI/review/merge 数据路径。

## 8. M1-4 Duplicate 恢复复测结果

- 公开契约：AO v0.12.9 的 `ConversationMessageResponse` 和 live Snapshot 均确认不回显 `clientMessageId`，但 user message 稳定提供 `role="user"`、`origin="human"`、完整 `text` 和 `turnId`。
- 恢复规则：Planner 与 Worker 共用同一确定性路径。只有 `role=user`、`origin=human`、完整正文严格相等且 `turnId` 为非空字符串的消息恰好一条时才复用 turn；零条、多条、非 human、正文冲突或无 `turnId` 均 fail closed。
- 幂等输入：`audit_id` 可由调用方固定，Planner/Worker 的 `ClientMessageID` 由 auditId 和阶段确定性生成。恢复时不重发新 ID，不按最近消息、sequence 或模糊文本猜测，也不读取 SQLite。
- live 结果：使用同 Project、带就绪标记的唯一 Chat-mode Planner 与 Worker，首次固定参数运行得到 `LOCAL_FIX` 与 Worker ACK；完全相同的再次运行在两阶段均收到 `Duplicate=true`，分别从公开 Snapshot 恢复已有 turn，最终退出码为 0 并返回原 ACK。
- 无副作用：复测前后 Planner/Worker 的 turn 和 message 总数不变，两个测试 worktree 保持干净、HEAD 不变、PR 数不变，项目 active Worker 数不变。
- 结论：M1-4 的正常闭环、两阶段 Duplicate 恢复和 fail-closed 边界均已通过离线测试与 live AO 验证；M1 最小 AO Adapter 完成。SSE、自动重试、数据库、Observer、Auditor 和 Integration Gate 不在本阶段实现范围内。

## 9. M2-2 Auditor 三阶段 live 与重复恢复结果

- 接口与版本：2026-08-28 再次确认上游 tag `v0.12.9`、commit `4cbb4b6ced1ad93f79641a2347d2342f1ffd218a` 的 Session、Conversation message/snapshot 和 workspace files 公开路由，并在同版本 live daemon 上执行；没有调用内部 Go service、SQLite、终端注入或 Worker 生命周期写接口。
- 唯一会话：在同一 Project 中按 Conversation READY 标记和期望 kind 唯一识别一个 Auditor worker、一个 Planner orchestrator 和另一个目标 Worker。三者 ID 不同，均为 Chat mode、idle、未 blocked/terminated；实验前后 active session ID 集合相同。
- workspace 真实形态：v0.12.9 的 `GET /api/v1/sessions/{id}/workspace/files` 会返回 tracked 文件，并以 `status="unmodified"` 表示干净，而不是用空 `files` 数组表示干净。Auditor 门禁因此只把非 `unmodified` 条目视为 changed files，同时继续拒绝任何额外 commit、truncated 或非法响应。
- 第一次运行：固定 auditId `m2-2-live-auditor-loop-20260828` 的 audited `clao` 退出 0。Auditor 返回关联正确、包含 `recommendedDecision=LOCAL_FIX` 的合法单行 `AuditReport`；Planner 返回 `LOCAL_FIX`；目标 Worker 精确返回 `M2-2-WORKER-ACK m2-2-live-auditor-loop-20260828`。三阶段 turn ID 和确定性 ClientMessageID 均非空。
- 重复恢复：以完全相同参数和 auditId 第二次运行仍退出 0，并返回相同 AuditReport、PlannerDecision 和 ACK。Auditor、Planner、Worker 三阶段分别复用第一次的同一 turn；第二次前后三会话 turn/message 数保持 `2/4`、`16/44`、`2/4`，没有新增 provider turn。
- 无副作用：实验前后三会话始终保持 Chat/idle、未 terminated；workspace tracked 文件数保持不变且全部 `unmodified`，changed files、额外 commits 和 PR 均为 0。session 总数和 active session ID 集合不变，没有会话被创建、停止、恢复、委派或重新调度。
- 只读真实边界：AO Chat Agent 并非由 Adapter 放入 OS 级只读文件系统或进程沙箱。当前保证来自两层：发送给 Auditor/Planner/Worker 的明确提示约束，以及对公开 Session、Conversation、workspace/commit/PR 状态的执行前后核验。状态核验能检测已暴露的副作用，但不能等同于内核强制的只读隔离。
- 结论：M2-2 的 audited CLI、真实 Auditor → Planner → Worker 三阶段闭环、三阶段 Duplicate 恢复和已定义无副作用门禁均已通过；M2 完成。Observer、Integration Gate、SSE、并发/queued turn 和其他 live 决策分支仍按后续里程碑处理。

## 10. M3-2 Observer 自动触发 live 结果

- 接口与版本：2026-08-28 在锁定的 AO Desktop v0.12.9 上，仅使用公开 Session、Conversation message/snapshot 和 workspace files REST 路由执行；没有使用 SSE、内部 Go service、SQLite、终端注入、Worker 生命周期或 delegation 接口，也没有修改 AO 源码。
- 唯一会话：在同一 Project 中按 `role=assistant`、`origin=provider` 的精确 READY 文本唯一识别 Auditor `closed-loop-agent-orchestrator-14`、Planner `closed-loop-agent-orchestrator-1` 和 Worker `closed-loop-agent-orchestrator-15`。实验前三者均为 Chat/idle、未 terminated，Auditor 与 Worker changed files、额外 commits 和 PR 均为 0。
- 自动触发：以前台 observed CLI 和 root auditId `m3-2-live-observed-loop-20260828` 启动。完成初始 Observation 后，只向目标 Worker 发送一次设置消息 `M3-2-DEMO-START m3-2-live-observed-loop-20260828`；AO 接受为一个新 turn，未调用 session 创建、停止、恢复或委派接口。
- 第一轮：Observer 检测设置 turn 完成产生 `MILESTONE`，确定性 cycle auditId 为 `m3-2-live-observed-loop-20260828:4f83f40b-af2b-57bb-bc39-4be8d9eb8fc6`。Auditor 返回 `LOCAL_FIX`，Planner 返回 `LOCAL_FIX`，Worker 返回 `M3-2-WORKER-ACK m3-2-live-observed-loop-20260828`；三阶段 turn 和 ClientMessageID 均进入结果。
- 第二轮：`LOCAL_FIX` 后立即重新捕获 Worker，Observer 检测新增 completed turn 并再次产生 `MILESTONE`；新 cycle auditId 为 `m3-2-live-observed-loop-20260828:f6108cbb-b2c9-5cf6-a6eb-de5763c6fbbf`。AuditRequest evidence 包含上一轮 Worker response，Auditor 返回 `PASS`，Planner 返回 `PASS`，没有向 Worker 再投递 instruction。
- 结果与副作用：CLI stdout 为单个合法 `ObservedLoopResult` JSON，`auditCount=2`、`termination=PASS`，整体退出码 0。实验前后 Project 的 15 个 session ID 完全相同，三会话最终均为 Chat/idle、未 terminated；Auditor、Planner、Worker changed files、额外 commits 和 PR 均为 0。没有创建、停止、恢复、委派或调度 Worker。
- active Worker 边界：公开 AO Session 的 `activity.state` 仍是状态门禁事实来源。observed 审计阶段只放宽目标 Worker 为 `active|idle|waiting_input`，Auditor/Planner 仍要求 `idle|waiting_input`；只有 Planner `LOCAL_FIX` 才轮询 Worker 到安全投递态，blocked/exited/terminated 或等待超时不强行注入并转 `HUMAN`。本次 live 的两个 milestone 在 Worker 回到 idle 后触发；active `STALL` 审计与安全等待/拒绝投递边界由离线 MockTransport 测试覆盖，尚未在 live AO 人为制造 300 秒停滞。
- 恢复边界：cycle auditId 只由 root auditId、Worker session id、trigger 和规范化进展签名生成；相同 Observation 重跑会复用现有 Auditor/Planner/Worker ClientMessageID 与公开 Snapshot Duplicate 恢复路径，不新增本地数据库或 JSON 状态文件。
- 结论：M3-2 的前台有界 Observer → Auditor → Planner → Worker → Observer 自动闭环已通过离线测试与 live AO 两轮验证；M3 完成。Integration Gate、SSE、后台服务、持久化、并发/queued turn、TUI 和完整 PR/CI/review/merge 路径仍未实现。

## 11. M4-2 Integration Gate 合并结果与失败恢复

- 合并目标：2026-08-29 使用原始、干净的 `main` checkout，HEAD 为已合并 PR #14 的 commit `b3d3004e9187c6144044fafc080d650a6e55fbd3`。正式 Gate 前后 `git status --porcelain` 均为空，HEAD 未变化；Gate 不执行 merge、checkout、reset 或 PR 操作。
- 会话基线：按 READY 文本和 kind 唯一选择 Auditor `closed-loop-agent-orchestrator-17`、Planner `closed-loop-agent-orchestrator-1`、Worker `closed-loop-agent-orchestrator-18`。三者均为 Chat/idle、未 terminated；Project 共 18 个 session ID。Auditor 与 Worker workspace 无 changed files、额外 commits 或 PR。
- 真实通过路径：Gate 顺序运行 `.venv\Scripts\python.exe -m pytest`、`-m compileall -q src`、`-m pip check`；分别得到 286 项测试通过、exit 0、exit 0，整体 `gate.passed=true`、CLI 退出 0、`gateAuditId=null`、`auditedResult=null`。三目标 turn/message 数保持 `1/2`、`24/64`、`1/2`，没有创建 AOClient 或发送新消息。
- 稳定失败证据：固定 root auditId `m4-2-live-gate-failure-20260829` 只运行输出 `M4-2-GATE-FAIL` 后 exit 7 的命令。完整 Gate result 保留实际 duration 和有界输出；AuditRequest evidence 包含 commit、`passed=false`、failure reason、argv、`exit_code=7`、`timed_out=false`、失败 stdout/stderr，不含 duration 或成功 step 输出。
- 第一次反馈：确定性 gate auditId 为 `m4-2-live-gate-failure-20260829:dbc2451e-a7d3-5846-8577-3a0a0bb139a3`。Auditor turn `86556cdf-85a6-460b-a7c6-8a8be06f8198` 返回 `recommendedDecision=LOCAL_FIX`；Planner turn `2256db72-3b72-4a21-8ccd-94ddab469407` 返回 `LOCAL_FIX`；Worker turn `954c6fb3-5f66-4a88-9a91-49a8b0d61db0` 返回 `M4-2-GATE-ACK m4-2-live-gate-failure-20260829:dbc2451e-a7d3-5846-8577-3a0a0bb139a3`。CLI 退出 3。
- Duplicate 恢复：第二次使用完全相同的 root auditId、任务契约、Gate repo 与命令。两次完整 Gate result 的 duration 不同，但稳定 evidence、gate auditId、三阶段 ClientMessageID、turn ID 和 provider turn ID 完全相同；第二次从公开 Conversation Snapshot 恢复原 AuditReport、PlannerDecision 与 ACK，没有新增 provider turn。恢复仍只适用于完整用户消息正文严格相等且唯一匹配的既有边界，参数或正文变化时 fail closed。
- 无副作用：第二次前后三会话 turn/message 数保持 `2/4`、`25/66`、`2/4`，Project 的 18 个 session ID 不变；三会话保持 Chat/idle、未 terminated，Auditor/Worker changed files、额外 commits 和 PR 为 0，三个目标 worktree HEAD/clean 状态与 Gate checkout 均不变。没有创建、停止、恢复、委派、调度或额外消息其他 Worker。
- 结论：真实 merged-main pass 与 Gate failure → Auditor → Planner → Worker 已闭环验证；Gate 成功完全绕过 Agent，失败复用既有 AO Chat/Conversation 与三阶段 Duplicate 恢复，不增加后台服务、数据库或新 Agent。
