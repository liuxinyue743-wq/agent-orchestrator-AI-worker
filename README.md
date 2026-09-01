# CL_AO

CL_AO 是一个面向 Vibe Coding 的闭环多智能体软件开发系统。它复用 Agent Orchestrator（AO）管理 Worker、Git worktree 与会话生命周期，在其上增加 Mission 拆解、确定性事件观察、独立语义审计、自动反馈、状态恢复以及最终 Gate/Verifier 验收。

> 当前版本：`v0.2.0-beta`（Windows 本地技术预览版）  
> 已验证底座：Agent Orchestrator `v0.12.9`  
> 发布边界：适合比赛演示、课程项目和受控软件原型；尚未按生产级软件或任意环境零配置运行进行认证。

## 能力概览

- Web 面板创建、停止、恢复和查看 Mission；
- Planner 将自然语言目标拆成最多两个有依赖关系的子任务；
- AO 在独立 worktree 中启动 Worker；
- Observer 检测重复失败、停滞、超时和预算阈值；
- Auditor 根据任务、diff、测试和事件证据给出 `PASS / LOCAL_FIX / REPLAN / HUMAN`；
- Planner 将修补或重规划动作自动送回 Worker；
- StateStore 使用 SQLite 持久化 Mission、Task、预算、审计和状态迁移；
- Gate 运行确定性测试；Verifier 对最终或高风险结果进行独立复核；
- 模型提供方可配置为 Claude CLI、Kimi CLI 或 Codex CLI。

## 实际控制路径

```text
Web Panel
    ↓
MissionController ─────────→ AO / Worker / worktree
    │
    ├─ Planner / Auditor / Verifier Providers
    ├─ Observer / Gate / Action Executor
    └─ StateStore（CL_AO 的权威运行状态）
             ↓
     Event Projector / Timeline（派生展示）
```

`StateStore` 负责恢复与决策；Markdown、JSONL 和拓扑时间线仅用于展示与审计，不作为第二套状态源。

## 快速开始（Windows 10/11 x64）

### 1. 前置条件

- Python 3.10 或更高版本；
- Git；
- 至少一个已安装并完成认证的编码 Agent CLI：Claude Code、Codex 或 Kimi；
- Agent Orchestrator `v0.12.9`。

源码仓库不把 130 MB 的 AO 安装器提交进普通 Git 历史。运行下面的脚本会从 AO 官方 GitHub Release 下载经过锁定和哈希校验的安装器：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-AO.ps1
```

如需下载后立即启动安装器：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-AO.ps1 -RunInstaller
```

固定版本、官方地址和 SHA-256 见 [`AO_VERSION.json`](AO_VERSION.json)。GitHub Release 打包流程会把同一官方安装器作为发布资产的一部分，因此最终用户不需要自行寻找版本。

### 2. 安装 CL_AO

双击：

```text
安装.bat
```

该脚本会创建 `.venv`、安装依赖并运行离线测试。测试失败时安装不会被标记为成功。

### 3. 配置 AO

先运行 `powershell -ExecutionPolicy Bypass -File .\scripts\Initialize-Demo.ps1` 初始化示例 Git 仓库，再打开 AO 注册目标仓库。推荐先使用 [`demo/closed-loop-demo`](demo/closed-loop-demo)。

若 CL_AO 无法自动发现 AO，设置：

```powershell
$env:CLAO_AO_BIN  = "C:\path\to\ao.exe"
$env:CLAO_AO_DATA = "C:\path\to\ao-data"
```

CL_AO 不携带任何开发机 `ao-data`。首次使用时由 AO 创建空白数据目录和本机认证状态。

### 4. 启动面板

双击：

```text
启动面板.bat
```

默认打开 `http://127.0.0.1:7100`。在面板中选择 AO 已注册的目标项目，填写任务目标、验收条件、允许路径和 Gate 命令后启动 Mission。

## 模型配置

配置文件为 [`config/providers.json`](config/providers.json)，示例见 [`config/providers.example.json`](config/providers.example.json)。默认使用本机 Claude CLI 的默认模型，不写入任何 API Key。GLM、Kimi、Codex 等需要用户先在对应 CLI 或网关中完成认证。

不要把密钥、Token 或含密码的 PostgreSQL DSN 提交到 Git；`config/storage.json` 已被 `.gitignore` 排除。

## 测试

本发布候选在清理后的源码目录中执行：

```powershell
set PYTHONPATH=src
python -m pytest -q
```

结果：`334 passed`。该结果验证离线控制逻辑；AO、Windows GUI、外部模型和全新电脑的端到端环境仍需各使用者按文档完成配置。

## 仓库中不包含的内容

以下内容被有意排除：

- `ao-data/`、AO 数据库、Cookies、Electron Profile；
- `.venv/`、缓存、日志和历史 runtime；
- 历史 worktree、真实模型会话和开发提示记录；
- 用户密钥与认证数据；
- AO 二进制本体（普通 Git 对大文件有限制；由安装脚本和 Release 资产提供）。

## 维护者发布到 GitHub

已配置 GitHub CLI 的维护者可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Publish-To-GitHub.ps1
```

脚本会把清洁源码推送到 `release/cl-ao-v0.2.0-beta`，创建 Pull Request，并创建预发布；AO v0.12.9 官方 EXE 作为独立 Release asset 上传，不进入普通 Git 历史。

## 文档

- [用户操作说明](操作说明.md)
- [项目架构](docs/ARCHITECTURE-v0.2.md)
- [真实架构与审计修正](docs/真实架构与审计修正.md)
- [首次初始化与安装 AO](docs/首次初始化与安装AO.md)
- [发布状态与限制](RELEASE_STATUS.md)
- [第三方软件说明](THIRD_PARTY_NOTICES.md)

## 许可证

CL_AO 源码采用 MIT License，见 [`LICENSE`](LICENSE)。

Agent Orchestrator 是独立的上游项目，版权归其作者所有，采用 Apache License 2.0。CL_AO 不声称拥有 AO；锁定安装器来自 AO 官方 Release，相关说明与许可证见 [`third_party/agent-orchestrator`](third_party/agent-orchestrator)。
