# 比赛演示

普通用户首次安装、通用角色模板、Session ID 获取和四种 CLI 模式见 [`QUICKSTART.md`](QUICKSTART.md)。本页角色契约只用于受控比赛演示，不应作为普通项目模板。

## 演示目标与 3 分钟流程

用一个前台 PowerShell 入口展示现有 Integration Gate 的两条路径：确定性检查全部通过时直接结束；稳定的 exit 7 失败时，把 Gate 证据送入只读 Auditor → 唯一 Planner → Worker 闭环。脚本只校验结果，不修改产品功能，也不管理 AO Session 或 Git 生命周期。

1. **0:00–0:30，准备。** 在 AO Desktop 中展示三个已就绪的 Chat 会话和干净的 Gate checkout，打开本页并确认参数。
2. **0:30–1:30，Pass。** 运行 `Scenario Pass`，展示 pytest、compileall、pip check 顺序通过，以及 `gate.passed=true`、`auditedResult=null`。
3. **1:30–2:30，Fail。** 运行 `Scenario Fail`，展示 exit 7 证据进入三阶段闭环，Planner 返回 `LOCAL_FIX`，Worker 返回精确 ACK。
4. **2:30–3:00，闭环与边界。** 展示已有 Observer 两轮证据，并说明 Gate 不会自动 merge 或自动重跑。

## 准备

- AO Desktop 固定使用 **v0.12.9**，目标项目已注册；提前准备 Auditor、Planner、Worker 三个 **Chat** 会话，记录各自 Session ID，并确保会话处于 `idle` 或 `waiting_input`、未 terminated。
- 使用 Python 3.11+。脚本所在仓库和 `GateRepo` 均已执行 `python -m venv .venv` 与 `.\.venv\Scripts\python.exe -m pip install -e ".[test]"`；脚本固定复用仓库内 `.venv\Scripts\clao.exe`，并把 `GateRepo` 内 `.venv\Scripts\python.exe` 解析为绝对路径后作为每条 Gate 命令的 executable。
- `GateRepo` 必须是准备好的干净 Git checkout；`git status --porcelain` 应为空。脚本不会替你 checkout、merge 或清理工作区。
- H1-3 已从全新 clone 完成 Scenario All 的 Pass、Fail 和相同 RunId 的 Duplicate 恢复彩排，Gate checkout 全程保持 clean。
- 彩排发现的两个问题均已修复：PowerShell 5.1 现在能保真传递 JSON argv，Gate 命令也会使用解析后的绝对 GateRepo Python 路径。

## 三个 Chat 会话的演示契约

把以下短契约分别交给提前创建的会话。它们约束演示回复，不授予文件或生命周期操作权限。

**Auditor**

> 你是只读 Auditor。只根据收到的 AuditRequest 和 Gate evidence 返回一行合法 AuditReport JSON，不修改文件、不执行命令、不管理 Session。当且仅当某个已执行 Gate step 的 stdout 精确为 DEMO-GATE-FAIL 且 exit_code=7 时，推荐 LOCAL_FIX。其他任何 Gate failure 都推荐 HUMAN，不向 Worker 提供修复建议，也不建议安装依赖或执行修复。

**Planner**

> 你是唯一 Planner。只返回一行合法 PlannerDecision JSON，不修改文件或管理其他 Session。当且仅当某个已执行 Gate step 的 stdout 精确为 DEMO-GATE-FAIL 且 exit_code=7 时，返回 LOCAL_FIX；instruction 要求目标 Worker 只回复 `DEMO-GATE-ACK <auditId>`，其中 `<auditId>` 必须替换为收到的 gate auditId。其他 Gate failure 返回 HUMAN，不向 Worker 投递 instruction。

**Worker**

> 你是演示 Worker。不修改文件、不执行 Git 或 Session 生命周期操作。收到本次 LOCAL_FIX instruction 后，只回复 instruction 指定的精确 `DEMO-GATE-ACK <gateAuditId>`，不添加其他文字。

这里的“只读 Auditor”是提示契约加 AO 公开状态前后核验，不是 OS 级只读沙箱。

## 完整调用示例

从脚本所在仓库根目录运行；参数不包含任何预置 Session ID、本机绝对路径或密钥。`RunId` 可省略，省略时脚本生成唯一值；显式提供可让同一组参数复测 Duplicate 恢复。

```powershell
.\scripts\demo.ps1 `
  -AuditorSession "<auditor-session-id>" `
  -PlannerSession "<planner-session-id>" `
  -WorkerSession "<worker-session-id>" `
  -GateRepo (Resolve-Path ".").Path `
  -RunId "competition-demo-20260829" `
  -Scenario All
```

`Scenario` 接受 `Pass`、`Fail`、`All`，默认 `All`。脚本捕获 `clao` 的单个 JSON stdout，只向终端输出阶段摘要；退出码、字段或精确 ACK 不符合契约时，脚本退出非零。Pass 路径不会创建 AO client 或给三个会话发消息，但 CLI 仍要求三个 Session 参数。

## 预期输出

Pass：

```text
[demo] RunId: competition-demo-20260829
[demo] Pass: running pytest, compileall, and pip check
[demo] Pass: OK (exit=0, gate.passed=true, auditedResult=null)
[demo] Complete
```

Fail（`gateAuditId` 由 RunId、Gate commit 与稳定失败证据确定性生成）：

```text
[demo] RunId: competition-demo-20260829
[demo] Fail: running controlled exit 7 command
[demo] Fail: OK (exit=3, decision=LOCAL_FIX, worker=DEMO-GATE-ACK <gateAuditId>)
[demo] Complete
```

## Observer 两轮自动闭环的已有证据

M3-2 已在 AO Desktop v0.12.9 live 验证前台两轮闭环：首个 Worker `MILESTONE` 触发 Auditor/Planner `LOCAL_FIX`，Worker 返回 `M3-2-WORKER-ACK m3-2-live-observed-loop-20260828`；新增 completed turn 形成第二个 `MILESTONE`，Auditor/Planner 随后返回 `PASS`。最终 stdout 是合法 `ObservedLoopResult`，`auditCount=2`、退出码 0；实验前后 Session 集合、changed files、额外 commit 和 PR 均不变，也没有 Session 生命周期或委派副作用。详细记录见 [`AO_INTEGRATION.md`](AO_INTEGRATION.md) 的 M3-2 与 M4-2 live 证据。

## 真实边界

- Auditor 不是 OS 级只读沙箱；只读性来自提示契约、公开 API 使用边界和运行前后证据核验。
- Gate failure 后只形成 Auditor → Planner → Worker 反馈；系统不会自动 merge，也不会自动重跑 Gate。
- Auditor、Planner、Worker 会话必须提前准备并保持安全状态；脚本不创建、停止、恢复、委派或调度 Session。
- 系统不新增数据库、后台服务或新 Agent；Observer 与 Gate 都是前台、确定性的普通程序。
- 脚本不执行 merge、checkout、reset、commit 或 push，也不在仓库中写入结果文件。
