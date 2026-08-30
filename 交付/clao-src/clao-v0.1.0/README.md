# closed-loop-agent-orchestrator

基于 Agent Orchestrator（AO）的闭环多智能体软件开发系统。

核心闭环：**规划 → 执行 → 审计 → 重规划**。

## 5 分钟快速开始

已验证环境为 Windows、Windows PowerShell 5.1、Python 3.12 和 AO Desktop v0.12.9；CL-AO 要求 Python 3.11+。使用时请区分三个部分：AO Desktop 是 Agent 运行与可视化平台，CL-AO 是单独安装的 Python CLI 控制层，用户目标项目是被观察、审计和执行 Gate 的 Git 仓库。

推荐目录布局：

```text
tools/
└── closed-loop-agent-orchestrator/
projects/
└── user-project/
```

在 PowerShell 中从源码安装 CL-AO：

```powershell
git clone https://github.com/zhaoshiyi4246/closed-loop-agent-orchestrator.git
cd closed-loop-agent-orchestrator
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\clao.exe --help
```

普通使用采用非 editable 安装，即 `python -m pip install .`；开发 CL-AO 或运行其自身测试时才使用 `python -m pip install -e ".[test]"`。虚拟环境无需激活，直接调用 `.venv\Scripts\clao.exe` 即可。

完整的 AO 安装、目标项目准备、三个 Chat 会话初始化、Session ID 获取、四种 CLI 模式、配置、退出码和故障排查见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)。比赛三分钟演示及其专用角色契约见 [`docs/DEMO.md`](docs/DEMO.md)；演示契约不是普通项目的通用模板。

## 当前状态

项目已完成 M0 可行性验证、**M1 — 最小 AO Adapter**、**M2 — 只读 Auditor 闭环**、**M3 — Deterministic Observer 前台有界自动闭环**、**M4 — 项目级 Integration Gate 闭环**和 H1 比赛演示与硬化。H1-3 已完成全新 clone 的 Pass、Fail 和 Duplicate 恢复彩排，下一步是冻结 v0.1.0。`run_observed_loop` 以固定间隔只读 AO 的 Session、Conversation Snapshot 和 workspace summary，纯确定性地产生 `REPEATED_FAILURE`、`MILESTONE` 或 `STALL` trigger，再复用 `run_audited_once` 驱动现有 Chat-mode Auditor、唯一 Planner 和目标 Worker。

M2-2 已将 audited 模式接入 `clao`，并在 AO v0.12.9 上完成真实 Auditor → Planner → Worker 三阶段 `LOCAL_FIX`、精确 ACK 和三阶段 Duplicate 恢复验证。测试前后三会话均无 changed files、额外 commit、PR 或生命周期副作用。该只读边界是提示约束加公开 REST 前后状态核验，不是 OS 级只读沙箱。

M3-2 已把 Observer 接入 `clao --observe`。循环要求显式 root auditId；每轮 cycle auditId 由 root auditId、Worker session id、trigger 和稳定进展签名确定性生成。`PASS`、`REPLAN`、`HUMAN` 立即终止；`LOCAL_FIX` 后重新观察 Worker；达到最大审计次数或总体时间上限时转 `HUMAN`，不建立后台服务或本地状态文件。

M4 已提供库级 `run_integration_gate` 与 `run_gated_once`，并接入 `clao --gate`。Gate 通过时直接返回且不创建 AOClient；配置命令执行前的 checkout/clean precondition failure 同样不调用 Agent。配置命令执行后失败时，程序以 root auditId、commit SHA 和稳定 Gate evidence 生成确定性 gate auditId，再复用现有 Auditor → Planner → Worker 闭环。

当前 Python 3.11+ 工程已实现 AO daemon runfile 发现、health/OpenAPI 检查、Project/Session 只读查询、Chat transport、项目协议、一次性闭环和 observed audited 闭环。一次性模式仍要求 Planner/Worker 安全空闲；observed 审计阶段允许目标 Worker 为 active、idle 或 waiting_input，但 Auditor 与 Planner 必须安全空闲。只有 `LOCAL_FIX` 才等待 Worker 进入 idle/waiting_input 后投递；blocked、exited、terminated 或等待超时都不强行注入并转人工边界。

`run_once` 可接收显式 auditId，未提供时生成 UUID；Planner 与 Worker 的 ClientMessageID 由 auditId 和阶段确定性生成。AO v0.12.9 的公开 Conversation Snapshot 不回显 `clientMessageId`。AO 报告 Duplicate 时，Adapter 只接受 Snapshot 中 `role=user`、`origin=human`、完整正文严格相等且带非空 `turnId` 的唯一消息；零条、多条、非 human、正文不一致或无 `turnId` 都会 fail closed，不重发新 ID，也不按最近消息、sequence 或模糊正文猜测 turn。

轮询以 turn 终态和对应 assistant message 为准，不以 `latestSequence` 增长判断完成。Observer 的停滞签名包含 turn id/state、message id/revision/streaming、conversation activity 的 id/revision/kind/status、workspace 文件状态、commit SHA 和 failure occurrence；failed command/error activity 使用 `activity:` 命名空间生成 fingerprint，与 `turn:` failure 来源区分。超时、失败 turn、非法 JSON、关联不一致或不安全状态都会明确失败，不猜测决策。当前仍未实现 SSE、后台服务或持久化；Adapter 不创建、恢复、停止或调度 Worker，也不自动 merge、checkout、reset、创建 PR 或创建 Worker。

## 当前部署方式

### AO Desktop

AO Desktop v0.12.9 是 Agent 运行和可视化平台，提供 Project、Session、Chat、worktree 和 PR 等能力。AO Desktop 需要单独安装，并在运行 CL-AO 前启动。

### CL-AO

CL-AO 是独立于 AO 的 Python CLI 控制层。当前可从本仓库源码在单独目录和 Python 虚拟环境中安装，不需要把整个 CL-AO 源码仓库复制到每个用户项目。安装后可直接调用该环境中的 `.venv\Scripts\clao.exe`，无需激活虚拟环境；激活虚拟环境只是把命令加入当前 shell `PATH` 的可选便利方式。

### 用户项目

用户项目只保留自身源码、测试、配置和可选的 `AGENTS.md`，不应复制 CL-AO 仓库的 `PLANS.md`、`docs/PROJECT.md` 或 `docs/AO_INTEGRATION.md`，也不应复制其他机器生成的 `.venv` 或 `.git`。目标项目通过 `--gate-repo` 等参数传给 CL-AO。

### 当前 v0.1 会话边界

- 当前 v0.1 不自动创建 AO Project、Planner、Auditor 或 Worker；用户需要预先创建三个 Chat 会话并提供 Session ID。
- Planner、Auditor 和 Worker 的角色契约只需一次性初始化，不需要在每次审计时重新填写。
- 初始编码任务仍由用户在 AO 中交给目标 Worker；在会话准备和初始任务下达后，CL-AO 可自动执行后续 Observer、Auditor、Planner feedback 和 Integration Gate 闭环。
- Planner 自动拆解高层任务、创建 Worker 和自动派工属于后续产品化方向，当前尚未实现。

## 离线 Integration Gate

`run_integration_gate(repo_root, commands, ...)` 先以只读 Git 命令记录 `HEAD` 并检查 `git status --porcelain`。工作区不干净时不会运行任何 Gate 命令；通过门禁后，每个命令都以 `shell=False` 在仓库根目录顺序运行，首个非零退出、启动错误或超时即停止。每个 stdout/stderr 独立保留配置字符上限内的前缀。

返回的 `IntegrationGateResult` 包含 `commit_sha`、`passed`、不可变的步骤结果和 `failure_reason`。结构化 dict 保留 duration 和所有有界输出；供 `AuditRequest` 使用的稳定 evidence 则排除 duration 和成功 step 输出，只保留 commit、passed、failure reason、各已执行 step 的 argv/exit code/timed out，以及失败 step 的有界 stdout/stderr。

## CLI

安装工程后可用 `clao` 运行兼容的直接 Planner → Worker 闭环：

```powershell
clao --planner-session planner-id --worker-session worker-id --audit-id audit-123 --finding "acceptance test failed" --evidence "pytest: one failure" --recommended-decision LOCAL_FIX
```

提供 `--auditor-session` 时进入 audited 模式；必须提供 `--task-goal` 和至少一个可重复的 `--acceptance-criterion`，`--constraint` 也可重复：

```powershell
clao --auditor-session auditor-id --planner-session planner-id --worker-session worker-id --audit-id audit-123 --task-goal "verify the target" --acceptance-criterion "Auditor returns a valid AuditReport" --acceptance-criterion "Worker returns the ACK" --constraint "do not modify files" --evidence "all sessions returned READY"
```

audited 模式不接受 `--finding` 或 `--recommended-decision`；直接模式仍要求 `--finding`。`--audit-id` 可选，但需要可重复运行时应显式固定；`--evidence`、`--poll-interval`、`--timeout` 和 `--runfile` 为两种模式共用。stdout 始终只写一个 JSON 对象；audited 结果额外包含完整 `auditReport`、Auditor turn/client message 元数据，并保留 Planner/Worker turn 和 Worker response。有效的非 `HUMAN` 结果退出码为 0，`HUMAN` 为 2，参数、运行或协议错误为 1。

在 audited 参数基础上提供 `--observe` 可进入前台 observed audited 模式；此模式必须显式提供 `--audit-id`，并支持 `--observe-interval`（默认 2 秒）、`--stall-threshold`（默认 300 秒）、`--failure-threshold`（默认 2）、`--max-audits`（默认 3）和 `--overall-timeout`（默认 600 秒）：

```powershell
clao --observe --auditor-session auditor-id --planner-session planner-id --worker-session worker-id --audit-id root-audit-123 --task-goal "verify the target" --acceptance-criterion "the Planner returns PASS"
```

observed stdout 为单个 `ObservedLoopResult` JSON，包含 root auditId、termination、reason、每轮 trigger/evidence、`AuditReport`、`PlannerDecision`、三阶段 turn/client message 元数据、Worker response 和总审计轮数。直接模式和一次性 audited 模式的参数与输出保持兼容；有效的非 `HUMAN` 结果退出码为 0，`HUMAN` 为 2，参数、运行或协议错误为 1。

在 audited 参数基础上提供 `--gate` 可验证已合并 checkout。此模式还要求显式 `--audit-id`、`--gate-repo` 和至少一个 JSON 字符串数组形式的 `--gate-command-json`；单命令默认超时 300 秒，单路输出默认保留 20000 字符：

```powershell
clao --gate --auditor-session auditor-id --planner-session planner-id --worker-session worker-id --audit-id root-gate-123 --task-goal "verify merged main" --acceptance-criterion "all integration checks pass" --gate-repo C:\path\to\merged-checkout --gate-command-json '[\"python\",\"-m\",\"pytest\"]'
```

Gate stdout 为单个 UTF-8 `GatedRunResult` JSON。通过时 `auditedResult=null` 且退出 0；失败并得到非 `HUMAN` 决策或 precondition failure 时退出 3；`HUMAN` 退出 2；参数、协议或运行错误退出 1。gate 模式不接受 `--observe`、`--finding` 或 `--recommended-decision`。

M1-4 live AO 使用固定 auditId 和完全相同参数复测成功：Planner 与 Worker 两阶段均收到 Duplicate、从公开 Snapshot 恢复已有 turn，最终退出码为 0 并返回原 Worker ACK。Planner/Worker turn 数没有增加，测试会话的文件、commit、PR 和 Worker 数均未变化。

M2-2 live AO 使用固定 auditId `m2-2-live-auditor-loop-20260828` 连续运行两次 audited CLI。第一次返回合法 `AuditReport`、`LOCAL_FIX` 和精确 ACK；第二次返回相同结果并复用 Auditor、Planner、Worker 三个原 turn，没有新增 provider turn。active session 集合、workspace changed files、额外 commits 和 PR 数均未变化。

M3-2 live AO 使用 root auditId `m3-2-live-observed-loop-20260828` 完成真实前台自动闭环。一次性设置消息产生首个 Worker milestone；第一轮 Auditor/Planner 返回 `LOCAL_FIX`，Worker 返回规定 ACK；Observer 随后检测新 completed turn，第二轮 Auditor/Planner 返回 `PASS`。stdout 为合法的两轮 `ObservedLoopResult`，退出码 0；实验前后 15 个 session ID 不变，三会话 changed files、额外 commits 和 PR 均为 0，也没有生命周期或委派副作用。

M4-2 在 PR #14 合并后的干净 `main` commit `b3d3004e9187c6144044fafc080d650a6e55fbd3` 上完成 live 验证。真实 pass 依次运行 pytest（286 项）、compileall 和 pip check，退出 0 且三会话没有新增消息。固定 root auditId 的可控 exit 7 失败两次得到相同 gate auditId、相同 Auditor/Planner/Worker turn 和原 ACK，第二次没有新增 provider turn；session 集合、workspace、commit、PR、Gate HEAD 和 clean 状态均未变化。

当前完整离线测试共 309 项，覆盖 direct、audited、observed、gate 四种 CLI 模式及其错误/恢复边界，全部不访问真实网络。

## 比赛演示

一键 Pass/Fail 演示的准备、角色契约、预期输出与真实边界见 [`docs/DEMO.md`](docs/DEMO.md)。

```powershell
.\scripts\demo.ps1 -AuditorSession "<auditor-session-id>" -PlannerSession "<planner-session-id>" -WorkerSession "<worker-session-id>" -GateRepo (Resolve-Path ".").Path -Scenario All
```

## 核心目标

在 AO 已有的编码智能体执行和 Git 工作流能力之上，补充四项能力：

1. 用普通程序低成本识别重复失败、停滞等异常；
2. 用一个只读 Auditor 判断任务是否真正满足目标；
3. 将审计证据自动返回唯一 Planner，由 Planner 决定下一步；
4. 在任务合并后进行项目级集成验收。

## 文档入口

- [`AGENTS.md`](AGENTS.md)：Codex 在本仓库中必须长期遵守的规则；
- [`docs/PROJECT.md`](docs/PROJECT.md)：当前权威项目设计；
- [`PLANS.md`](PLANS.md)：当前里程碑、任务和停止条件。

单次给 Codex 的任务提示词、对话记录和临时审计意见不写入仓库。
