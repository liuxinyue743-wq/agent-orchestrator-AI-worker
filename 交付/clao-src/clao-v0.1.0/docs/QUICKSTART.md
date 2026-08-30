# CL-AO v0.1 Windows 快速开始

## 1. 产品定位

CL-AO 是运行在 AO Desktop 同一台 Windows 电脑上的 Python CLI 控制层。它读取 AO 的公开 Session、Conversation Snapshot 和 workspace 状态，把 Observer、只读 Auditor、唯一 Planner、目标 Worker 与 Integration Gate 串成前台闭环。

请始终区分三个独立部分：

- **AO Desktop**：Agent 的运行和可视化平台，负责 Project、Session、Chat、worktree 和 PR 等能力。
- **CL-AO CLI**：本仓库安装出的 `clao.exe`，通过 AO daemon 的 loopback REST API 工作。
- **用户目标项目**：你真正要开发、观察和验证的 Git 仓库。

AO 与 CL-AO 不是同一个安装包。CL-AO 不包含 AO Desktop，也不会替代 AO 的界面、Agent 运行时或 Git 工作流。

## 2. 已验证平台和前置条件

| 项目 | 要求或已验证版本 |
|---|---|
| 操作系统 | Windows；当前已完成 Windows 全新 clone 彩排 |
| Shell | Windows PowerShell 5.1 已验证 |
| Python | 项目要求 3.11+；Python 3.12.7 已验证 |
| AO Desktop | 固定验证版本 v0.12.9 |
| Git | 必须可用；目标项目必须是 Git checkout |
| Coding Agent | 在 AO 中可用且支持 Chat interface；v0.1 优先验证 Codex Worker |

安装前还需要能够访问 GitHub 和 Python 包索引，并按 AO 自身说明完成所选 Coding Agent 的安装与认证。

推荐把工具和项目分开：

```text
%USERPROFILE%/
├── tools/
│   └── closed-loop-agent-orchestrator/
└── projects/
    └── user-project/
```

## 3. 安装并启动 AO Desktop v0.12.9

1. 打开 [Agent Orchestrator v0.12.9 Release](https://github.com/Untrivial-ai/agent-orchestrator/releases/tag/v0.12.9)。
2. 下载 Windows installer `Agent.Orchestrator.Setup.0.12.9.exe` 并完成安装。
3. 启动 AO Desktop，确认 About/版本信息为 v0.12.9。
4. 按 AO 的安装说明准备并认证一个支持 Chat 的 Coding Agent；本项目 v0.1 优先验证 Codex。
5. 后续运行 CL-AO 时保持 AO Desktop 已启动。Desktop 会启动本地 daemon，不要求系统中存在全局 `ao` 命令。

AO installer 必须单独下载；不要把它放入 CL-AO 源码包或未来的 CL-AO Release。

## 4. 下载并安装 CL-AO

在 PowerShell 中执行：

```powershell
$toolsRoot = Join-Path $env:USERPROFILE "tools"
New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null
Set-Location $toolsRoot

git clone https://github.com/zhaoshiyi4246/closed-loop-agent-orchestrator.git
Set-Location .\closed-loop-agent-orchestrator
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\clao.exe --help
```

`clao --help` 会输出一个含 `help` 字段的 JSON 对象并退出 0。普通用户应使用非 editable 安装：

```powershell
.\.venv\Scripts\python.exe -m pip install .
```

只有开发 CL-AO 或运行 CL-AO 自身测试时才安装测试依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

CL-AO 只安装在这一个工具目录中，不复制到每个目标项目。无需激活虚拟环境，可以始终直接调用 `.venv\Scripts\clao.exe`；激活虚拟环境只是让 `clao` 临时进入当前 PowerShell `PATH` 的便利方式。不要把本机生成的 `.venv` 或 `.git` 复制到另一台电脑。

## 5. 准备目标项目

目标项目只需要：

- 自身源码；
- 自身测试环境和可执行的验证命令；
- 一个正常的 Git checkout；
- 可选的项目级 `AGENTS.md`。

例如把项目放在独立目录：

```powershell
$projectsRoot = Join-Path $env:USERPROFILE "projects"
New-Item -ItemType Directory -Force -Path $projectsRoot | Out-Null
Set-Location $projectsRoot
$projectRepositoryUrl = "https://github.com/your-org/your-project.git"
git clone $projectRepositoryUrl user-project
Set-Location .\user-project
git status --short --branch
```

按目标项目自己的文档建立测试环境；它与 CL-AO 的 `.venv` 是两个不同环境。Gate 需要目标项目的测试命令可直接运行，并要求被验证的 checkout 保持 clean。

不要向目标项目复制 CL-AO 仓库的 `PLANS.md`、`docs/PROJECT.md` 或 `docs/AO_INTEGRATION.md`，也不要复制其他仓库或机器的 `.venv`、`.git`。当前 v0.1 没有 `.clao.toml` 或其他项目配置文件；Session ID、任务目标、验收条件、阈值和 Gate 命令均通过 CLI 参数提供。

## 6. 在 AO 中注册目标项目

1. 启动 AO Desktop v0.12.9。
2. 使用 **New project**，选择目标项目的本地 Git 根目录，例如 `C:\projects\user-project`。
3. 按 AO 界面选择已安装的 Worker 与 Orchestrator Agent。
4. 创建后打开该 Project，确认 AO 展示的是目标仓库，而不是 CL-AO 工具仓库。

CL-AO v0.1 不会执行这一步，也不会自动创建 AO Project。

## 7. 创建 Planner、Auditor、Worker 三个 Chat 会话

在同一个 AO Project 中准备三个彼此不同的 Session：

1. **Planner**：使用该 Project 已有的 Orchestrator；若尚未创建，使用 **Spawn Orchestrator**。必须选择 Chat interface，最终 `kind` 为 `orchestrator`。
2. **Auditor**：使用 **New task** 创建一个 Chat Worker，可命名为 `CL-AO Auditor`。最终 `kind` 为 `worker`。
3. **Worker**：再使用 **New task** 创建一个 Chat Worker，可命名为 `CL-AO Worker`。最终 `kind` 为 `worker`。

如果 Project 已有唯一 Orchestrator，就复用它，不再创建第二个 Planner。Auditor 和 Worker 是两个独立 Worker Session；不要把同一个 Session ID 同时用于两个角色。

创建后先等待三个会话均未 terminated，且 activity 为 `idle` 或 `waiting_input`。Observed 模式启动后的观察阶段允许目标 Worker 随工作进入 `active`，但 Auditor 与 Planner 仍须保持安全空闲状态。

## 8. 一次性角色初始化

下面的通用模板分别粘贴到三个 Chat 会话中。每个会话只需初始化一次，不要在每轮审计时重复粘贴。模板不使用比赛演示中的专用失败文本或 ACK。

### Auditor 模板

```text
你是本项目唯一的只读 Auditor。收到以“AuditRequest:”开头的请求后，只根据该 AuditRequest、其中的任务目标、验收条件、约束和 evidence 进行独立语义审计。只返回一行 AuditReport JSON，不添加 Markdown 或其他文字。JSON 必须包含 auditId、targetSessionId、finding、evidence、recommendedDecision；recommendedDecision 只能是 PASS、LOCAL_FIX、REPLAN、HUMAN。不要修改文件、创建 commit、执行命令，也不要创建、停止、恢复、委派、调度或消息通知任何 Session。
```

### Planner 模板

```text
你是本项目唯一拥有项目级决策权的 Planner。收到以“AuditReport:”开头的报告后，根据报告返回一行 PlannerDecision JSON，不添加 Markdown 或其他文字。JSON 必须包含 auditId、decision、targetSessionId；decision 只能是 PASS、LOCAL_FIX、REPLAN、HUMAN。只有 LOCAL_FIX 才提供非空 instruction，且 instruction 只交给报告中的目标 Worker，必须边界清晰、可执行并限于当前任务；reason 可选。你不充当 Auditor，不重新进行独立语义审计，也不修改文件或管理 Session 生命周期。
```

### Worker 模板

```text
你是当前目标项目的执行 Worker。每次工作前先阅读目标项目的 AGENTS.md（如存在）和当前具体任务，只执行用户或 Planner 分配的边界清晰工作，遵守修改范围、约束和验收条件，并返回真实验证证据。你不承担项目级规划，不充当语义 Auditor，不自行创建或调度其他 Worker；遇到范围、权限或高风险决策时明确请求人工处理。
```

初始化消息发出后，等待对应会话完成回复并回到 `idle` 或 `waiting_input`。这些模板是普通项目的通用角色约束；[`DEMO.md`](DEMO.md) 中的契约只用于受控比赛演示。

## 9. 获取和确认 Session ID

AO v0.12.9 的 Session 列表公开 REST 响应已通过现有 `AOClient.list_sessions(active=True)` 实机验证。当前不要依赖未经确认的 UI 复制操作；在 CL-AO 安装目录运行下面的只读 PowerShell/Python 命令。它不读取 SQLite，也不修改 AO 状态：

```powershell
Set-Location "$env:USERPROFILE\tools\closed-loop-agent-orchestrator"

$script = @'
import json
from closed_loop_agent_orchestrator.ao_client import AOClient

with AOClient() as client:
    for session in client.list_sessions(active=True):
        print(json.dumps({
            "sessionId": session.get("id"),
            "title": session.get("displayName") or session.get("branch") or "(untitled)",
            "kind": session.get("kind"),
            "projectId": session.get("projectId"),
            "interface": session.get("mode"),
            "activity": (session.get("activity") or {}).get("state"),
            "terminated": session.get("isTerminated"),
        }, ensure_ascii=False))
'@

$script | .\.venv\Scripts\python.exe -
```

AO v0.12.9 的真实字段名是 `mode`；上述命令将它显示为更直观的 `interface`。会话 `displayName` 是可选字段，因此标题为空时回退到 branch 或 `(untitled)`。

从输出中选择三个 Session，并逐项确认：

- 三个 `sessionId` 非空且彼此不同；
- 三者的 `projectId` 完全相同，并对应目标项目；
- Planner 的 `kind` 是 `orchestrator`；
- Auditor 和 Worker 的 `kind` 都是 `worker`；
- 三者的 `interface` 都是 `chat`；
- 三者的 `terminated` 都是 `false`；
- 首次检查及投递前，三者的 `activity` 为 `idle` 或 `waiting_input`。

把三个 ID 复制到本次 PowerShell 会话的普通变量中，不要写入仓库：

```powershell
$plannerSession = "<planner-session-id>"
$auditorSession = "<auditor-session-id>"
$workerSession = "<worker-session-id>"
```

## 10. 向 Worker 下达初始编码任务

当前 v0.1 的初始编码任务仍由用户在 AO Desktop 中直接发给目标 Worker，不由 CL-AO 自动生成。任务至少写清：

```text
目标：<要完成的具体结果>
修改范围：<允许修改的文件或目录>
约束：<不能做什么、依赖和安全边界>
验收条件：<可验证的完成条件>
验证证据：<必须运行并报告的命令或检查>
```

第一次真实使用建议按以下顺序熟悉闭环：

1. 在 AO 中给 Worker 下达一个明确编码任务；
2. Worker 完成并安全空闲后，用 audited 模式做一次语义审计；
3. 后续给 Worker 新任务或继续执行时，用 observed 模式进行前台持续观察和多轮闭环；
4. 代码合并到待验收 checkout 后，用 gate 模式执行项目级检查。

Direct 模式不是必经步骤；只有在你已经有明确 finding 时使用。

## 11. 选择 direct / audited / observed / gate 模式

| 模式 | 何时选择 | 主要行为 |
|---|---|---|
| direct | 已经有明确 finding，不需要 Auditor 再审计 | 将 finding 直接交给 Planner，必要时反馈目标 Worker |
| audited | 需要 Auditor 对目标和证据审计一次 | Auditor → Planner；仅 `LOCAL_FIX` 投递 Worker |
| observed | 需要持续自动观察 Worker | 前台 Observer 触发多轮 Auditor → Planner → Worker；受次数和总时间限制 |
| gate | 验证干净的已合并 checkout | 顺序运行确定性命令；命令失败后才进入 audited feedback |

不确定时，第一次语义检查使用 audited；正在执行并希望自动发现 milestone、重复失败或停滞时使用 observed；合并后使用 gate。

## 12. 运行示例

以下示例均可在 Windows PowerShell 5.1 中修改后运行。先设置公共变量：

```powershell
$clao = "C:\tools\closed-loop-agent-orchestrator\.venv\Scripts\clao.exe"
$plannerSession = "<planner-session-id>"
$auditorSession = "<auditor-session-id>"
$workerSession = "<worker-session-id>"
```

如果 CL-AO 安装在用户目录，请把 `$clao` 改为你的实际路径；无需激活虚拟环境。

### Direct：已有 finding

```powershell
& $clao `
  --planner-session $plannerSession `
  --worker-session $workerSession `
  --audit-id "direct-20260829-001" `
  --finding "The target test still fails after the latest Worker change." `
  --evidence "pytest reports one failing acceptance test." `
  --recommended-decision LOCAL_FIX
$LASTEXITCODE
```

### Audited：执行一次语义审计

```powershell
& $clao `
  --auditor-session $auditorSession `
  --planner-session $plannerSession `
  --worker-session $workerSession `
  --audit-id "audited-20260829-001" `
  --task-goal "Complete the requested user-visible behavior." `
  --acceptance-criterion "The target project's acceptance tests pass." `
  --acceptance-criterion "The implementation stays within the requested scope." `
  --constraint "Do not weaken tests." `
  --evidence "The Worker reports its changed files and verification results."
$LASTEXITCODE
```

### Observed：持续观察和多轮闭环

```powershell
& $clao `
  --observe `
  --auditor-session $auditorSession `
  --planner-session $plannerSession `
  --worker-session $workerSession `
  --audit-id "observed-20260829-001" `
  --task-goal "Complete the assigned coding task without repeated failure or stall." `
  --acceptance-criterion "The requested behavior and tests pass." `
  --constraint "Keep changes inside the assigned task." `
  --observe-interval 2 `
  --stall-threshold 300 `
  --failure-threshold 2 `
  --max-audits 3 `
  --overall-timeout 600
$LASTEXITCODE
```

Observed 是前台有界循环；保持该 PowerShell 窗口运行。它不会安装为后台服务。

### Gate：验证干净的合并 checkout

下面的 PowerShell 5.1 示例使用无空格路径，并先把目标项目 Python 解析为绝对路径。`Replace` 用于让 JSON 内的双引号完整到达 `clao.exe`：

```powershell
$gateRepo = "C:\projects\user-project"
$gatePython = (Resolve-Path (Join-Path $gateRepo ".venv\Scripts\python.exe")).Path
$gateCommandJson = (ConvertTo-Json -InputObject ([string[]] @(
  $gatePython,
  "-m",
  "pytest"
)) -Compress).Replace('"', '\"')

& $clao `
  --gate `
  --auditor-session $auditorSession `
  --planner-session $plannerSession `
  --worker-session $workerSession `
  --audit-id "gate-20260829-001" `
  --task-goal "Verify the merged target project." `
  --acceptance-criterion "All configured integration checks pass." `
  --constraint "Do not modify the Gate checkout." `
  --gate-repo $gateRepo `
  --gate-command-json $gateCommandJson
$LASTEXITCODE
```

非 Python 项目应把 JSON argv 改成该项目真实存在的验证命令。每增加一个 Gate 命令，就重复一次 `--gate-command-json`。Gate checkout 必须 clean；CL-AO 不会替你 checkout、merge 或清理工作区。

## 13. 配置方式

当前 v0.1 只有 CLI 参数，没有 `.clao.toml`、`clao init`、`clao bootstrap` 或自动 Session discovery。

| 配置类别 | 参数 |
|---|---|
| 会话 | `--planner-session`、`--worker-session`；audited/observed/gate 还需 `--auditor-session` |
| 任务契约 | `--task-goal`、可重复的 `--acceptance-criterion`、可重复的 `--constraint` |
| 证据与幂等 | 可重复的 `--evidence`、`--audit-id` |
| 普通轮询 | `--poll-interval`（默认 2 秒）、`--timeout`（默认 90 秒） |
| Observer | `--observe-interval`、`--stall-threshold`、`--failure-threshold`、`--max-audits`、`--overall-timeout` |
| Gate | `--gate-repo`、可重复的 `--gate-command-json`、`--gate-timeout`、`--gate-output-limit` |
| AO daemon | `--runfile`；否则读取 `AO_RUN_FILE`，再否则读取 `%USERPROFILE%\.ao\running.json` |

Direct 和 audited 的 `--audit-id` 可省略，但希望安全重试时应显式提供；observed 和 gate 必须显式提供。相同 auditId 只用于完全相同正文和参数的重试；任务目标、证据、约束或 finding 改变时必须换新 auditId。

CL-AO stdout 始终是一个 JSON 对象。建议同时检查 JSON 内容和 `$LASTEXITCODE`，不要只看 Agent 的自然语言回复。

## 14. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | `--help`；或 direct/audited/observed 得到有效的非 `HUMAN` 结果；或 Gate 通过 |
| 1 | 参数、AO 连接、协议解析、超时或其他运行错误；stdout 包含 `error` JSON |
| 2 | Planner/observed/gate 结果到达 `HUMAN`，需要人工决策 |
| 3 | Gate 失败且不是 `HUMAN`；也包括在执行配置命令前发生的 Gate precondition failure |

Gate 退出 3 不表示 CL-AO 自身崩溃；应读取 stdout 中的 `gate.failure_reason`、steps 和可选 `auditedResult`。

## 15. 常见问题

### AO Desktop 未启动，或提示 runfile 不存在

先启动 AO Desktop，再检查默认 runfile：

```powershell
Test-Path (Join-Path $env:USERPROFILE ".ao\running.json")
```

如果 AO 使用非默认 runfile，通过 `--runfile <path>` 或环境变量 `AO_RUN_FILE` 显式提供。不要手工编造端口。

### Session ID 错误

重新运行第 9 节的只读列表命令，并复制 `sessionId` 字段。三个 ID 必须非空、不同，且都属于目标 Project。

### Session 不是 Chat mode

列表中的 `interface` 必须为 `chat`。当前闭环不会向 TUI Session 投递；请在 AO 中创建或切换为 Chat 会话后重新确认。

### Session 不属于同一 Project

三条记录的 `projectId` 必须完全相同。不要混用 CL-AO 工具仓库、目标仓库或其他 Project 的 Session。

### Session blocked、exited 或 terminated

CL-AO 会 fail closed，不强行投递。回到 AO 处理阻塞或由用户手动恢复/重建会话，等待其回到允许状态；CL-AO 不会自动恢复或替换 Session。

### GateRepo 不干净

检查：

```powershell
$gateRepo = "C:\projects\user-project"
git -C $gateRepo status --porcelain
```

有输出时不要运行 Gate。先人工完成当前改动，或改用一个准备好的 clean 合并 checkout。CL-AO 不会 reset、stash 或清理它。

### 虚拟环境不存在

如果缺少 CL-AO 的 `.venv\Scripts\clao.exe`，回到第 4 节重新创建工具虚拟环境并安装。如果 Gate 命令引用目标项目自己的 `.venv`，则按目标项目文档在目标 checkout 中单独创建；不要复制其他机器的虚拟环境。

### 使用相同 auditId，但请求正文已经改变

Duplicate 恢复要求相同阶段的消息正文严格一致。正文改变却复用 auditId 会明确失败，不会猜测或创建一个新 ID。为新 finding、任务目标、验收条件、约束或 evidence 使用新的 auditId。

### 退出码 0、1、2、3 怎么处理

- 0：读取 JSON 确认具体 decision/termination 或 `gate.passed`；
- 1：读取 `error.code` 和 `error.message`，修复参数、AO 或协议问题；
- 2：停止自动流程，由用户处理 `HUMAN` 原因；
- 3：读取 Gate 失败证据；若包含 audited feedback，再按 Planner 结论处理。

## 16. 当前边界

当前 v0.1：

- 不自动创建 AO Project 或 Session；
- 不自动初始化 Planner、Auditor、Worker 角色；
- 不自动拆解高层任务；
- 不自动创建 Worker 或自动派工；
- 不自动 merge、创建 PR、checkout、reset 或重跑 Gate；
- 不使用后台服务、数据库或消息队列；
- Auditor 不是 OS 级只读沙箱；只读性来自角色提示、公开 API 边界和执行前后 workspace 证据核验。

Planner 自动拆解高层目标、创建 Worker 和自动派工属于后续产品化方向，当前尚未实现。第一次使用时仍需用户完成 Project/Session 创建、一次性角色初始化、Session ID 选择以及初始编码任务下达。
