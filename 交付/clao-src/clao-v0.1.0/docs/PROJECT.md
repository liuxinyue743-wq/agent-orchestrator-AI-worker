# 闭环多智能体软件开发系统：项目设计

- 状态：核心闭环已完成，下一阶段为比赛演示与硬化
- 更新日期：2026-08-29
- 项目仓库：`closed-loop-agent-orchestrator`

## 一、文档定位

本文件是当前项目目标、边界和架构的权威说明。

- Codex 的长期工作规则见根目录 `AGENTS.md`；
- 当前里程碑和实施状态见根目录 `PLANS.md`；
- 若旧版 PDF 或其他说明与本文件冲突，以本文件为准。

## 二、问题

Agent Orchestrator（AO）已经面向多个编码智能体提供任务会话、隔离工作区和 Git 开发流程等能力，但“Worker 一直在运行”并不等于“项目正在逼近目标”。

实际开发中仍可能出现：

- 重复执行相似命令或反复遇到同一错误；
- 修改很多代码，却没有获得新的有效验收证据；
- 只修复表面症状或偏离任务目标；
- 多个任务分别通过，但合并后接口、依赖或整体行为不一致；
- 发生语义层失败后，仍需人工判断如何调整计划。

本项目解决的核心问题是：

> 如何低成本判断编码智能体是否真正取得有效进展；发现停滞、偏航或集成失败后，如何把证据自动返回唯一规划层，使项目继续执行直到达到可验证的完成状态。

## 三、项目目标

在复用 AO 现有执行能力的前提下，增加一个最小闭环：

1. 普通程序持续观察事件，并识别值得审计的异常或里程碑；
2. 一个只读 Auditor 根据任务目标、改动和验证证据作出独立判断；
3. Auditor 的结论自动返回唯一 Planner；
4. Planner 决定继续、局部修复、重规划或请求人工介入；
5. 全部任务通过后，再进行项目级集成验收。

核心流程为：

**规划 → 执行 → 审计 → 重规划 → 再执行**

## 四、范围

### 4.1 MVP 范围

- 一个本地 Git 仓库；
- 一个锁定版本的 AO；
- 一个项目级 Planner / Orchestrator；
- 按任务创建的 Codex Worker，最多并行两个；
- 一个只读 Auditor；
- 一个非 Agent 的 Deterministic Observer；
- 一个非 Agent 的 Integration Gate；
- 优先覆盖四类场景：重复失败、停滞、任务验收失败、集成失败。

### 4.2 明确不做

- 不训练或开发新的代码生成模型；
- 不重复实现 AO 已有的 Worker、worktree、branch、PR、CI、review 或终端能力；
- 不新增独立 Coordinator Agent；
- 不新增独立 Risk Agent 或常驻 Reviewer Agent；
- 不建立固定的 PM / Architect / Engineer / QA 角色团队；
- 不在 MVP 中支持多种编码智能体提供方；
- 不开发完整替代 AO 的 Dashboard；
- 不提前建设云端、多仓库、长期记忆或通用插件平台。

## 五、最小架构

### 5.1 Planner / Orchestrator

唯一拥有项目级控制权的 Agent，负责：

- 理解用户目标；
- 拆分任务并维护依赖；
- 创建和分配 Worker；
- 接收审计结论；
- 决定继续、局部修复、重规划或人工介入；
- 决定项目是否可以结束。

不得再增加第二个拥有同类控制权的 Agent。

### 5.2 Worker

按真实任务创建的执行单元，负责：

- 在自己的工作区完成边界清晰的编码任务；
- 运行必要测试并提交验证证据；
- 接收明确反馈并修复当前任务。

Worker 不负责项目级规划，也不是预先常驻的角色。

### 5.3 Deterministic Observer

普通程序，不是 Agent。负责：

- 汇总必要事件；
- 对错误和重复操作做去重或 fingerprint；
- 检测超时、重复失败、长时间无有效变化和预算阈值；
- 在异常或任务里程碑时触发 Auditor。

Observer 只负责低成本触发，不对任务是否真正完成作最终语义判断。

M3 的 Observer 从公开 Session、Conversation Snapshot 与 workspace summary 保存 activity、turn/message、changed files、commit SHA 和带 namespaced 稳定来源 ID 的失败 fingerprint。稳定进展签名包含 turn id/state、message id/revision/streaming、conversation activity 的 id/revision/kind/status、workspace 文件状态、commit SHA 和 failure occurrence；`latestSequence` 单独增长不算有效进展。failed command/error activity 以 `activity:` 命名空间生成 fingerprint，与 `turn:` failure 来源区分。

`run_observed_loop` 在前台固定间隔重新捕获 Observation，progress signature 或 Worker activity state 变化时重置 stall 起点；产生 trigger 后复用现有 `run_audited_once`，并在 `LOCAL_FIX` 后重新观察 Worker。cycle auditId 由显式 root auditId、Worker session id、trigger 和当前稳定进展签名确定性生成。循环受最大审计次数和总体时间限制，不使用后台服务、SSE、数据库或状态文件。

### 5.4 Auditor

唯一的独立语义审计 Agent，且必须只读。负责：

- 根据任务目标、范围和验收条件检查完成度；
- 判断是否偏航、只修复表面症状或引入明显跨任务风险；
- 对失败给出证据和原因。

Auditor 只允许输出四类结论：

- `PASS`：任务证据充分；
- `LOCAL_FIX`：方向仍有效，返回当前 Worker 局部修复；
- `REPLAN`：计划、任务拆分或执行路线需要调整；
- `HUMAN`：需要人工作出范围、权限或高风险决策。

Auditor 不直接修改代码或执行状态。

### 5.5 Integration Gate

普通验证流程，不是 Agent。所有任务分别通过后，在合并结果上执行必要的构建、集成测试、契约测试或 E2E 验证。

集成失败时，证据返回 Auditor 和 Planner，重新进入项目级闭环。任务级通过不能替代项目级完成。

M4-1 的离线核心只在 clean Git checkout 上运行：先用只读 Git 命令记录 `HEAD` 并检查 `git status --porcelain`，再以 `subprocess`、`shell=False` 在仓库根目录顺序执行调用方提供的显式 argv。首个非零退出、启动错误或超时即停止；每步只记录 argv、退出码、超时、monotonic 时长及字符上限内的 stdout/stderr。结果只包含确定性事实，并可生成结构化 dict 和后续 `AuditRequest` 可使用的 evidence 文本。

M4-2 的 `run_gated_once` 先运行上述确定性 Gate。Gate 通过或在任何配置命令执行前发生 checkout/clean precondition failure 时不创建 AOClient、不读取 Session，也不发送 Agent 消息；配置命令执行后失败时，以 root auditId、commit SHA 和不含 duration/成功 step 输出的稳定 evidence 生成确定性 gate auditId，再复用既有只读 Auditor → Planner → Worker 闭环。程序不重新执行 Gate，也不自动 merge、checkout、reset、创建 PR 或创建 Worker。

## 六、任务契约

Planner 派发给 Worker 的每个任务至少应明确：

- 目标；
- 修改范围；
- 依赖与限制；
- 验收条件；
- 必须提供的验证证据。

Worker 的自报完成或单一测试通过不能单独作为任务完成依据。

## 七、两级反馈闭环

### 7.1 L0：局部执行闭环

适用于可以明确归属于当前 Worker 的局部问题，例如编译失败、测试失败、明确的 review 意见或 merge conflict。

这类反馈优先复用 AO 已有生命周期能力，直接返回拥有该任务的 Worker，不升级为项目级重规划。

### 7.2 L1：项目级语义闭环

适用于无法由 CI 单独判断的问题，例如：

- 高活动但无有效进展；
- 实现偏离任务目标；
- 根因判断错误；
- 任务拆分或依赖关系失效；
- 集成后出现跨任务问题。

流程固定为：

**Observer 触发 → Auditor 审计 → Planner 决策 → Worker 执行 → 再验证**

所有项目级控制动作只由 Planner 执行。

## 八、项目新增能力与 AO 边界

### 8.1 计划复用 AO 的能力

以下能力直接复用 AO；M0 已通过 AO v0.12.9 的锁定源码审查和核心闭环运行实验确认复用边界，具体证据、限制和未验证项见 `docs/AO_INTEGRATION.md`：

- 编码智能体会话；
- 独立 branch / worktree；
- 任务与终端状态；
- Git diff、PR、CI、review 和 merge 生命周期；
- 项目级 orchestrator 与 Worker 管理。

### 8.2 本项目新增的能力

只新增四项核心能力：

1. 低成本异常触发；
2. 独立语义审计；
3. 审计结果自动返回 Planner；
4. 项目级 Integration Gate。

任何新增模块都必须证明不能由上述四项、AO 或普通程序覆盖。

## 九、验证与比赛展示

### 9.1 最低可演示结果

系统至少应能完成两个闭环演示：

1. Worker 重复失败或停滞后，Observer 触发审计，Planner 自动调整并继续执行；
2. 两个任务各自通过但合并失败后，Integration Gate 捕获问题并自动形成修复任务。

### 9.2 对照方式

使用相同 AO 版本、Codex 模型和任务，对比：

- Baseline：AO 原有工作流；
- Ours：AO + Observer + Auditor + Planner Feedback + Integration Gate。

关注指标：

- 最终任务完成率；
- 人工介入次数；
- 重复失败次数；
- 达到正确结果的时间和模型调用成本；
- 审计误报和无效重规划次数。

## 十、当前已确定与待验证内容

### 10.1 已确定的设计决策

- 只保留一个 Planner；
- Worker 按任务创建，MVP 最多两个并行；
- 只保留一个只读 Auditor；
- 确定性检测不使用 LLM；
- 所有审计结果返回 Planner；
- 项目结束前必须经过 Integration Gate；
- Integration Gate 必须以普通确定性程序执行；M4-1 只接受显式 argv，并在 clean checkout 的已记录 commit 上以 `shell=False` 顺序运行；
- MVP 优先使用 Codex Worker；
- AO v0.12.9 的外部读取与 Chat message 入口足以支持第一版；
- 第一版采用 Python 外部 Adapter，不修改或 fork AO；
- 第一版不使用数据库；
- REST Session 和 Conversation Snapshot 轮询是正确性路径，SSE 后置为可选优化。
- `clao` 已同时支持直接 Planner → Worker 模式和 audited Auditor → Planner → Worker 模式；后者输出 `AuditReport` 与三阶段 turn 元数据。
- AO v0.12.9 已真实验证三阶段 `LOCAL_FIX`、精确 Worker ACK 和三阶段 Duplicate turn 恢复；active session、workspace changed files、额外 commits 和 PR 数均无副作用。
- Auditor 只读边界由明确提示约束和执行前后公开 REST 状态核验共同提供，不是 OS 级只读沙箱。
- `clao --observe` 已实现前台有界自动闭环并输出 `ObservedLoopResult`；direct 和一次性 audited 模式保持兼容。
- observed 审计阶段允许目标 Worker 为 active、idle 或 waiting_input，但 Auditor 与 Planner 仍须 idle/waiting_input；只有 `LOCAL_FIX` 才等待 Worker 安全空闲后投递，blocked/exited/terminated 或等待超时不强行注入。
- AO v0.12.9 已真实完成两轮 `MILESTONE → LOCAL_FIX → MILESTONE → PASS`；session 集合、changed files、额外 commits 和 PR 均无副作用。
- `clao --gate` 已在 PR #14 合并后的干净 `main` 上真实验证：pass 不调用 Agent；可控 exit 7 失败进入 Auditor → Planner → Worker；完全相同的第二次运行复用三阶段 turn，且 session/workspace/commit/PR 与 Gate checkout 均无副作用。

### 10.2 后续验证内容

- SSE 断线重放；
- 并发或 queued turn；
- 长时运行与超时恢复；
- 除第一版明确处理的 blocked、terminated、exited 外的其他状态门禁；
- `PASS`、`REPLAN`、`HUMAN` 等其他 `PlannerDecision` 分支；
- TUI mode 自动闭环；
- 完整 PR、CI、review 和 merge 数据路径。

这些事项不阻塞 M0 完成，但在进入对应实现前仍须取得运行证据。

## 十一、第一版实现路线

### 11.1 运行形态

- 程序是与 AO Desktop 同机运行的外部 CLI Adapter，通过 AO daemon 的 loopback REST API 工作。
- 不修改 AO 源码，不 fork AO，不导入 AO 内部 Go package，不直接读取 SQLite。
- AO CLI 只用于人工诊断，不作为正式 Adapter 主入口。

### 11.2 技术栈与依赖边界

- Python 最低版本为 3.11；本机已确认可用的 `python` 版本为 3.12.7。
- 第一版采用同步实现，不引入 `asyncio` 或并发框架。
- 计划的唯一运行时第三方依赖是 `httpx`，计划的唯一测试依赖是 `pytest`；M0 阶段未添加或安装这些依赖。
- 使用标准库的 `dataclasses`、`enum`、`json`、`argparse`、`logging`、`subprocess` 和 `time`。
- 暂不引入 Pydantic、FastAPI、数据库、消息队列、SSE 库、Web UI 或 Agent 框架。

### 11.3 正确性模型

- REST Session 和 Conversation Snapshot 是状态与结果的事实来源。
- 第一版通过低频轮询读取 Planner 和 Worker 的 Conversation Snapshot；默认轮询间隔为 2 秒，单次等待默认上限为 90 秒，未来代码允许参数覆盖。
- SSE 不进入第一版正确性路径，只作为后续可选的提前唤醒优化；即使 SSE 断开或缺少事件，轮询闭环仍必须工作。
- 超时、快照读取失败、JSON 无法解析、`auditId` 无法关联，或目标处于 blocked、terminated、exited 状态时，必须返回 `HUMAN` 或明确错误，不得猜测 Planner 决策。

### 11.4 状态存储

- 第一版仅使用单次进程内状态，`auditId` 和 `ClientMessageID` 必须具有唯一性。
- 重启恢复首先依靠 AO Conversation Snapshot 和 `auditId` 重新检查。
- 第一版不建立 SQLite、JSON 状态库或后台服务。只有后续实验证明进程内状态不能满足恢复需求时，才考虑最小持久化。

### 11.5 消息协议

最小 `AuditRequest` 包含：

- `auditId`；
- `targetSessionId`；
- `taskGoal`；
- `acceptanceCriteria`；
- `constraints`；
- `evidence`。

最小 `AuditReport` 包含：

- `auditId`；
- `targetSessionId`；
- `finding`；
- `evidence`；
- `recommendedDecision`；M2 Auditor 输出必须提供，M1 兼容入口仍允许省略。

最小 `PlannerDecision` 包含：

- `auditId`；
- `decision`；
- `targetSessionId`；
- `instruction`，可选；
- `reason`，可选。

`decision` 只允许 `PASS`、`LOCAL_FIX`、`REPLAN`、`HUMAN`。`AuditReport` 与 `PlannerDecision` 是本项目协议，不是 AO 原生 DTO。

### 11.6 最小代码边界

第一版只规划以下六个必要逻辑部分，不继续拆分：

1. **AO Client**：daemon 发现；OpenAPI/健康检查；Project、Session、Conversation 读取；Chat message 发送。
2. **Protocol**：`AuditRequest`；`AuditReport`；`PlannerDecision`；最小字段验证。
3. **Loop Runner**：向 Planner 发送 `AuditReport`；轮询并解析 `PlannerDecision`；对 Worker 做状态门禁；发送反馈并等待结果；在前台把 Observer trigger 或已执行命令的 Gate failure 接入 audited loop；超时和 `HUMAN` 兜底。
4. **CLI**：兼容 direct、audited、observed 输入，并提供要求显式 root auditId 与合并 checkout/命令的 gate 模式；输出结构化结果和退出码。
5. **Deterministic Observer**：只读捕获 Worker 的 Session、Conversation 和 workspace 快照；用确定性规则识别重复失败、里程碑和停滞，只返回 trigger 与 evidence，不作语义审计决策。
6. **Integration Gate**：在 clean Git checkout 的已记录 commit 上顺序执行显式 argv；完整结果记录退出码、超时、时长和有界输出，稳定审计 evidence 排除耗时与成功 step 输出，Gate 本身不作语义决策。

不得为这六项提前增加 repository、service、manager、gateway、event bus、plugin 或其他抽象层。
