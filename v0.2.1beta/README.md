# CL_AO v0.2.1-rc1

Closed-Loop Agent Orchestrator

CL_AO 是构建在 Agent Orchestrator（AO）之上的本地闭环软件开发控制层。它把用户的
Mission 交给受控的 Codex Worker，在确定性观察、Gate 和最终验证后生成可审计结果。
**状态：修补候选，尚未重跑本次修改后的 Windows/AO 真实失败恢复验收。**
本次修改说明见 [`docs/PATCH_NOTES_0.2.1-rc1.md`](docs/PATCH_NOTES_0.2.1-rc1.md)。
当前架构见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 系统要求

- Windows；
- CPython 3.12.x；
- Git；
- AO Desktop 0.12.9（基准版本），daemon 已启动；
- Codex CLI，并已通过 `codex login` 使用 ChatGPT 登录。

CLAO 不安装或启动 Git、AO Desktop、Codex CLI，也不读取 API Key 作为默认认证方式。

## 准备 AO Project

先在 AO 中注册要使用的 Git repository。针对已验证的 AO Desktop 0.12.9，Project
必须具有名为 `origin` 的 remote 和可用的 remote-backed base ref：

- 显式 `defaultBranch=<branch>` 时，`refs/remotes/origin/<branch>` 必须存在；
- `defaultBranch=auto` 时，`refs/remotes/origin/HEAD` 必须指向一个可解析的
  remote branch；
- 没有 `origin` 的 local-only repository 当前不受支持。

`origin` 可以指向 GitHub/GitLab，也可以指向完全本地的 bare Git repository；这一
要求本身不需要互联网。CLAO 不会自动执行 `git fetch`、添加 remote、设置 remote
HEAD 或修改 AO Project config。Panel 与 CLI 会在创建 runtime 前通过共享 preflight
报告缺失项。

## 安装与启动

最简单的方式是双击 `启动CLAO.bat`。启动器会在需要时调用 `bootstrap.ps1` 创建本
目录的 `.venv`、安装 `requirements.txt` 中锁定的依赖，然后在浏览器中打开 Panel：

```text
http://127.0.0.1:7100/
```

也可以手动 bootstrap：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

bootstrap 只管理本目录的 Python 环境，不连接 AO，也不调用模型。

## 使用 Panel

1. 启动 AO Desktop；
2. 双击 `启动CLAO.bat`；
3. 在 Project selector 中选择已注册的 AO Git Project；
4. 填写目标、允许路径、验收条件和 Gate 命令；
5. 保持默认 `max_subtasks=1`，只有确有独立并行收益时才选择 2；
6. 启动 Mission，并在页面中查看 Task、Gate、Verifier 和 timeline。

Panel 不会构造 demo Project，也不会替用户注册或修改 AO Project。

## 使用 CLI

复制一个 sample，替换 `project_id` 和 `mission_id`，然后运行：

```powershell
$env:PYTHONPATH = (Resolve-Path ".\src").Path
.\.venv\Scripts\python.exe .\run_mission.py .\tasks\mission-quick.json `
  --poll-seconds 5 --cap-seconds 1200
```

`tasks/mission-quick.json` 与 `tasks/e2e-smoke.json` 都是模板，其中
`REPLACE_WITH_AO_PROJECT_ID` 必须替换为真实 AO Project ID。每次新运行应使用唯一
`mission_id`。

仅查看确定性单任务计划可使用：

```powershell
.\.venv\Scripts\python.exe .\run_mission.py .\tasks\e2e-smoke.json --dry-run
```

当 `max_subtasks=1` 时，dry-run 不连接 AO、不调用 Codex 模型，也不创建 runtime。

## 结果与 SCM 边界

每个 Mission 的状态和证据位于：

```text
runtime/<mission-id>/
```

`MISSION_DONE` 表示 integration 结果已经通过 Final Gate 和 Mission Verifier。结果保留
在 `runtime/<mission-id>/integration`，不会自动修改目标 repository 的 `main` 或
`master`，也不会自动 push `origin`。将结果交付到目标主分支始终需要用户显式操作。

## 故障排查

- **AO unavailable**：启动 AO Desktop，确认默认 `~/.ao/running.json` 可用；若 `ao`
  不在 PATH，可为当前进程设置 `CLAO_AO_BIN`。
- **Codex login**：运行 `codex login status`，确认显示 ChatGPT 登录。
- **origin/default branch**：确认 Project 有 `origin`，运行
  `git rev-parse refs/remotes/origin/<branch>`；auto 模式还需确认
  `git symbolic-ref refs/remotes/origin/HEAD`。
- **spawn failure**：Panel/StateStore 会显示经过脱敏且有长度上限的根因摘要；凭据、
  prompt 和用户主目录不会写入该摘要。

## 本地自检

```powershell
$env:PATH = "$(Resolve-Path '.\.venv\Scripts');$env:PATH"
$env:PYTHONPATH = (Resolve-Path ".\src").Path
.\.venv\Scripts\python.exe -m pytest .\tests -q
.\.venv\Scripts\python.exe -m compileall -q .\src .\panel .\run_mission.py
```


## 本次修补的本机检查

保持现有 AO 安装、登录和数据目录不动，在本目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Test-Local.ps1
```

默认只重建/检查本目录 Python 环境、完整运行离线测试、编译检查和单任务 dry-run；
不会创建 AO Worker、调用模型、修改目标 Git 仓库或推送 GitHub。
传给本地 GLM/Claude 的真实失败恢复验收任务见
[`docs/LOCAL_VALIDATION_TASK.md`](docs/LOCAL_VALIDATION_TASK.md)。

完整 JSON Schema 校验为必需依赖，缺少时明确失败，不降级为顶层字段检查。
已有 `.venv` 也必须运行一次 bootstrap 安装新增依赖。

## 授权与外部依赖

CL_AO 的既有授权文本见 [`LICENSE`](LICENSE)，来源和第三方边界见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。AO 安装器可作为独立
Release 附件提供，但本候选源码不含 AO 程序或真实用户数据。
