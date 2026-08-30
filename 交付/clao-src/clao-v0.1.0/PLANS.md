# 当前计划

- 更新日期：2026-08-29
- 当前阶段：比赛演示与硬化（已完成）
- 当前任务：**H1-3 — 全新 clone 现场彩排（已完成）**
- 下一步：**冻结 v0.1.0、生成干净源码包、录制初评视频并填写产品信息表**

## 一、里程碑目标

M3 已在 M2 只读 Auditor 闭环基础上完成非 Agent 的 Deterministic Observer 离线核心，以及前台、有界、可恢复的 Observer → Auditor → Planner → Worker → Observer 自动循环。M4 已完成非 Agent 的确定性 Integration Gate、CLI 接入、真实合并结果验证及 Gate failure → Auditor → Planner → Worker 闭环。

## 二、M0 完成情况

**M0 — AO 集成可行性验证：已完成。**

M0 已锁定 AO v0.12.9，完成静态接口审查与 Planner → Worker 运行实验，并确定采用 Python 外部 CLI Adapter。AO 的外部读取与 Chat message 入口足以支持第一版，不需要修改或 fork AO；详细证据见 `docs/AO_INTEGRATION.md`。

### M0 任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| M0-1 | 初始化项目文档 | 已完成 | `README.md`、`AGENTS.md`、`docs/PROJECT.md`、`PLANS.md` 一致 |
| M0-2 | 锁定并运行 AO 版本 | 已完成 | 可复现的版本与本地启动记录 |
| M0-3 | 审查 AO 集成面 | 已完成 | 源码或文档证据、可用接口与限制 |
| M0-4 | 完成最小可行性实验 | 已完成 | Planner 决策与单 Worker 反馈的运行时闭环 |
| M0-5 | 确定实现路线 | 已完成 | Python 外部 Adapter、实现边界和 M1 任务序列 |

### M0 已验证基线

- AO：锁定 tag `v0.12.9`，上游 commit `4cbb4b6ced1ad93f79641a2347d2342f1ffd218a`。
- Windows installer：`Agent.Orchestrator.Setup.0.12.9.exe`，已用于安装 AO Desktop v0.12.9。
- 本机环境：Microsoft Windows `10.0.26200`（X64）；PowerShell `5.1.26100.9168`；Git `2.55.0.windows.3`；GitHub CLI `2.97.0`，认证就绪且可访问目标仓库；Codex CLI `0.150.1`。
- AO 就绪状态：
  - AO Desktop v0.12.9 已安装并成功启动；
  - AO 已成功注册当前项目；
  - AO 已成功创建一个 Codex Worker；
  - Worker 位于独立任务分支和独立 worktree。
- 全局 `ao` 命令不是桌面应用运行的必要条件。
- M0-3 已确认锁定版本的真实 REST、Conversation、SSE 和状态接口边界。
- M0-4 已通过外部 HTTP 操作完成 Planner 决策、单 Worker 反馈与 Conversation Snapshot 读取闭环；SSE 未被证明足以承担正确性路径。

## 三、M1 任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| M1-1 | 初始化最小 Python 工程，实现 daemon 发现、健康/OpenAPI 检查和只读 Project/Session 查询 | 已完成 | 最小工程骨架与只读 AO Client；离线测试和 live smoke 已通过 |
| M1-2 | 实现 Chat message 发送、Conversation Snapshot 轮询、ClientMessageID 幂等和协议解析 | 已完成 | 可跟踪消息与协议读取路径；离线测试已通过 |
| M1-3 | 实现一次性 AuditReport → PlannerDecision → Worker 的 CLI 闭环 | 已完成 | 同步 `run_once`、`clao` 入口和 fake/MockTransport 离线测试 |
| M1-4 | 针对成功、重复提交、超时、非法决策、blocked 状态编写测试，并在 live AO 上复测 | 已完成 | 离线恢复/边界测试与 live AO 两阶段 Duplicate 恢复均已通过 |

## 四、M2 任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| M2-1 | 实现只读 Auditor 协议、workspace 门禁和 Auditor → Planner → Worker 库级闭环 | 已完成 | `AuditRequest`、只读 workspace summary、`run_audited_once` 与 156 项离线测试 |
| M2-2 | 接入 audited CLI，并在锁定的 AO v0.12.9 上验证 Auditor 只读约束与真实三阶段闭环 | 已完成 | CLI 双模式、185 项离线测试、三阶段 live 与 Duplicate 恢复、workspace/commit/PR/session 无副作用证据 |

## 五、M3 任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| M3-1 | 实现非 Agent 的 Deterministic Observer 一次性只读快照与纯确定性 trigger 评估 | 已完成 | `Observation`、只读 `capture_observation`、`evaluate_observation` 与完整离线测试 |
| M3-2 | 将 Observer 接入前台有界、可恢复的自动触发 audited loop | 已完成 | `run_observed_loop`、observed CLI、确定性 cycle auditId、active Worker 安全投递边界、261 项离线测试与两轮 live PASS |

## 六、M4 任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| M4-1 | 实现非 Agent 的确定性 Integration Gate 离线核心 | 已完成 | clean checkout 门禁、显式 argv 顺序执行、超时/截断、不可变结果、结构化 dict、evidence 与 fake runner 离线测试 |
| M4-2 | 将 Gate 接入真实合并结果验证及 Auditor/Planner 闭环 | 已完成 | `GatedRunResult`、gate CLI、稳定 evidence/auditId、真实 pass/failure 与三阶段 Duplicate 恢复 |

## 七、H1 比赛演示与硬化任务

| 编号 | 任务 | 状态 | 产出 |
|---|---|---|---|
| H1-1 | 最小 CI 与干净安装验收 | 已完成 | 单一 Windows GitHub Actions job 在 Python 3.12 clean runner 上安装工程并运行离线验收命令 |
| H1-2 | 一键演示与比赛说明 | 已完成 | `scripts/demo.ps1` 一键 Pass/Fail/All 验证与 `docs/DEMO.md` 比赛说明 |
| H1-3 | 全新 clone 现场彩排 | 已完成 | 全新 clone 的 Scenario All、Fail Duplicate 恢复、clean Gate checkout 与两项彩排修复均已验证 |

H1 只用于比赛演示与发布硬化，不是新的架构里程碑，不建立 M5。

## 八、当前明确不做

- M3 observed loop 保持前台有界运行，不增加后台服务、SSE、数据库或 JSON 状态文件；
- Gate 不增加后台服务，不自动 merge、checkout、reset、创建 PR、创建 Worker 或重新运行 Gate；
- 不开发新的 Dashboard；
- 不接入第二种编码智能体；
- 不引入数据库、消息队列、MCP Server 或新的 Agent 框架；
- 不为尚未验证的 AO 接口编写生产代码。

## 九、当前决策

- 设计遵循“如无必要，勿增实体”；
- MVP 只保留一个 Planner、按需 Worker、一个只读 Auditor；
- 确定性检测由普通程序完成；
- MVP 优先使用 Codex Worker，最多两个并行 Worker；
- 单次 Codex 提示词和临时审计意见不进入仓库；
- AO 外部读取和 Chat message 入口足以支持第一版；
- 不修改或 fork AO，也不导入 AO 内部 Go package 或直接读取 SQLite；
- 第一版使用与 AO Desktop 同机运行的 Python 外部 CLI Adapter；
- 第一版仅使用进程内状态，不使用数据库；
- REST Session 和 Conversation Snapshot 轮询是正确性路径，SSE 后置为可选唤醒优化。
- Observer 的进展签名包含 turn id/state、message id/revision/streaming、conversation activity 的 id/revision/kind/status、workspace 文件状态、commit SHA 和 namespaced failure occurrence，不把 `latestSequence` 单独增长视为有效进展；
- failed command/error activity 使用 `activity:` 来源命名空间生成 failure fingerprint，与 `turn:` 来源区分；
- `evaluate_observation` 只返回 `REPEATED_FAILURE`、`MILESTONE`、`STALL` trigger 和 evidence，不返回审计决策；`run_observed_loop` 只在 trigger 后调用现有 `run_audited_once`；
- observed 模式的 cycle auditId 由 root auditId、Worker session id、trigger 和稳定进展签名确定性生成，从而复用三阶段 ClientMessageID/Duplicate 恢复路径；
- Auditor 与 Planner 始终要求 idle/waiting_input；目标 Worker 在审计阶段可为 active、idle 或 waiting_input，只有 `LOCAL_FIX` 才等待安全空闲后投递，blocked/exited/terminated 或等待超时不强行注入；
- Integration Gate 只在 clean Git checkout 的已记录 commit 上以 `shell=False` 顺序执行显式 argv；只返回命令、退出码、超时、时长和有界输出等确定性事实，不返回 Auditor 或 Planner 决策；
- Gate 的 AuditRequest evidence 包含 commit、passed、failure reason、各已执行 step 的 argv/exit code/timed out 和失败 step 有界输出，不包含 duration 或成功 step 输出；gate auditId 由 root auditId、commit 和该稳定 evidence 确定性生成；
- 锁定的 AO v0.12.9 Conversation Snapshot 不回显 `clientMessageId`，但公开消息字段包含 `role`、`origin`、`text` 和 `turnId`；Duplicate 恢复只接受 `role=user`、`origin=human`、完整正文严格相等且 `turnId` 非空的唯一消息，零条或多条匹配均 fail closed，不按最近消息、sequence 或内部存储猜测。

## 十、后续验证项

- SSE 断线重放；
- 并发或 queued turn；
- 长时运行与超时恢复；
- 除第一版明确处理的 blocked、terminated、exited 外的其他状态门禁；
- `PASS`、`REPLAN`、`HUMAN` 等决策分支的 live AO 复测；
- TUI mode 自动闭环；
- 完整 PR、CI、review 和 merge 数据路径。

## 十一、完成证据与下一步

`clao` 保留直接和一次性 audited 模式，并增加要求显式 root auditId 的 `--observe`。`run_observed_loop` 捕获初始 Observation、固定间隔轮询，progress signature 或 Worker activity state 变化时重置 stall 起点；trigger evidence、基础 evidence 和上一次 `LOCAL_FIX` Worker response 一并进入现有 audited loop。`PASS`、`REPLAN`、`HUMAN` 立即终止；达到 `max-audits` 或 `overall-timeout` 转 `HUMAN`。

离线 fake/`httpx.MockTransport` 共 261 项测试，覆盖 direct/audited 兼容、无 trigger 总体超时、两轮 MILESTONE、REPEATED_FAILURE、active STALL、message/activity/workspace/commit 进展重置、LOCAL_FIX 安全等待、blocked/exited/terminated 拒绝投递、三类终止决策、max-audits、确定性 cycle auditId、三阶段 Duplicate 恢复及 observed CLI；测试不访问真实网络。

M3-2 使用 root auditId `m3-2-live-observed-loop-20260828` 在同一 Project 的唯一 READY Auditor、Planner 和 Worker 上完成真实运行。一次性设置消息产生首个 MILESTONE；第一轮 cycle `4f83f40b-af2b-57bb-bc39-4be8d9eb8fc6` 的 Auditor/Planner 返回 `LOCAL_FIX`，Worker 返回 `M3-2-WORKER-ACK m3-2-live-observed-loop-20260828`；Observer 检测新增 completed turn 后，第二轮 cycle `f6108cbb-b2c9-5cf6-a6eb-de5763c6fbbf` 的 Auditor/Planner 返回 `PASS`。stdout 为合法 `ObservedLoopResult`，`auditCount=2`，退出码 0。

实验前后 Project 的 15 个 session ID 完全相同，三目标会话均为 Chat/idle 且未 terminated；changed files、额外 commits 和 PR 均为 0。没有创建、停止、恢复、委派或调度 Worker。M3 至此完成。

M4-1 新增 `GateStepResult`、`IntegrationGateResult` 与 `run_integration_gate`。Gate 先以 `git rev-parse HEAD` 记录 commit，再以 `git status --porcelain` 执行 clean checkout 门禁；随后仅用 `subprocess`、`shell=False` 在仓库根目录依次执行显式 argv，首个非零退出、启动错误或超时即停止，并对 stdout/stderr 保留配置字符上限内的确定性前缀。结果可转换为结构化 dict 和后续 `AuditRequest` 可使用的 evidence 文本，但不包含审计或规划决策。

M4-1 的新增测试全部注入 fake runner，不执行真实项目命令。当前共 282 项离线测试通过，并已实际验证以下命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pip check
```

M4-2 新增唯一必要结果类型 `GatedRunResult` 与同步 `run_gated_once`，并把 `--gate`、`--gate-repo`、可重复 `--gate-command-json`、timeout/output limit 接入 `clao`。pass 与初始 precondition failure 均不创建 AOClient；已执行命令的 failure 才使用稳定 evidence 调用既有 `run_audited_once`。direct、audited、observed 三种模式保持兼容；gate 退出码为 pass 0、非 `HUMAN` failure 3、`HUMAN` 2、参数/协议/运行错误 1。

当前共 309 项离线测试通过，覆盖稳定 evidence、确定性 gate auditId、pass/precondition 无 Agent 路径、失败三阶段闭环与 Duplicate 恢复、CLI JSON/模式前置校验、退出码 0/1/2/3，以及 Windows legacy console 的 UTF-8 单 JSON 输出边界；所有离线测试均不访问真实网络。

live Gate 目标为 PR #14 合并后的干净 `main` commit `b3d3004e9187c6144044fafc080d650a6e55fbd3`。pass 依次运行 pytest（286 项）、compileall 与 pip check，退出 0 且 Auditor/Planner/Worker turn/message 数不变。固定 root auditId `m4-2-live-gate-failure-20260829` 的 exit 7 失败生成 gate auditId `m4-2-live-gate-failure-20260829:dbc2451e-a7d3-5846-8577-3a0a0bb139a3`；Auditor/Planner 返回 `LOCAL_FIX`，Worker 返回规定 ACK，CLI 退出 3。完全相同的第二次运行复用三阶段 turn 与 provider turn，三会话 turn/message 数保持 `2/4`、`25/66`、`2/4`。

实验前后 Project 的 18 个 session ID 完全相同；三目标会话均为 Chat/idle、未 terminated；Auditor 与 Worker changed files、额外 commits 和 PR 均为 0；三个目标 worktree、Gate repo HEAD 和 clean 状态均未变化。没有创建、停止、恢复、委派或调度 Worker。M4 至此完成，随后进入比赛演示与硬化，不启动新的架构里程碑。

H1-3 已从全新 clone 建立 Python 虚拟环境并安装 CL-AO，使用预备的三个 Chat 会话完成 Scenario All 的 Pass 和 Fail 真实彩排；使用相同 RunId 重跑 Fail 时通过 Duplicate 恢复原有三阶段 turn，Gate checkout 全程保持 clean。彩排中发现并修复了 PowerShell 5.1 JSON argv 传递问题（`928f9f1bf55fb68cd90db484959fc905555bbbb8`），以及从非 GateRepo 目录启动时 GateRepo Python 路径错误（`7f6eb6c348f17ef8c7ecc236ce5d16893e8c3542`）；最新 `main` CI 通过。H1 至此完成，下一步是冻结 v0.1.0、生成干净源码包、录制初评视频并填写产品信息表。
