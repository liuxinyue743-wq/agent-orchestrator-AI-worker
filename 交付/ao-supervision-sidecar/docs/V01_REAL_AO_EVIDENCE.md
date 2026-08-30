# V0.1 真实 AO 闭环集成证据

**状态：SUBMITTED**（真实闭环端到端跑通至 DONE；phase-4 claude-code 静默完成路径亦实机跑通至 DONE）。
记录时间：2026-08-28（phase-3）/ 2026-08-29（phase-4，见文末）。全部来自实际命令输出（E 盘）。

## 摘要

真实闭环在真实 AO worker / 真实 Claude(GLM-5.2) Auditor + Planner 上端到端跑通：

```
TASK_READY -> WORKER_RUNNING -> AUDIT_PENDING -> PLANNER_PENDING
-> LOCAL_FIX_PENDING -> WORKER_RETRYING
-> AUDIT_PENDING -> PLANNER_PENDING -> LOCAL_FIX_PENDING -> WORKER_RETRYING
-> GATE_PENDING -> DONE
```

- 真实 Auditor（`claude -p` headless，GLM-5.2，全工具禁用）判 `LOCAL_FIX`（conf 0.92）
- 真实 Planner（`claude -p` headless，GLM-5.2）输出 `SEND_LOCAL_FIX`，message 可执行
- 真实 `ao send` 把修复指令送进 worker 会话 `closed-loop-demo-5`
- worker（codex harness）实际实现了 `divide(a,b)`（app.py +6 行）
- Integration Gate 在 worker worktree 跑 `python -m pytest -q`：**exit 0，2 passed**
- 状态机推进至 `DONE`

## 环境

- AO daemon：`ready`，pid 41732，**动态端口 3001**（从 `ao-data/ao.run` 读取）
- Claude Code CLI：2.1.211；网关 `ANTHROPIC_BASE_URL`（ark 火山方舟），模型 `GLM-5.2`
  （auditor/planner 子进程内显式覆盖 `ANTHROPIC_MODEL=GLM-5.2`，
  绕开 settings.json 中指向已失效 deepseek-v4-pro 的默认值）
- Codex：0.150.1（worker harness）
- 项目 `closed-loop-demo`（single_repo，注册到 E 盘路径）

## Demo 仓库

- 路径：`E:\智理杯智能体大赛\closed-loop-demo`
- 初始 `app.py`：仅 `add(a,b)`；`tests/test_divide.py` 要求 `divide(6,3)==2` 且
  `divide(1,0)` 抛 `ValueError`。初始 2 failed（`ImportError: cannot import
  name 'divide'`）。
- git：init + commit `a83037f`

## Worker session

- `ao spawn --project closed-loop-demo --harness codex --name demo-worker
  --mode chat --prompt "...run pytest -q three times, do not modify code, reply DONE_RUNNING"`
- **WORKER_SESSION_ID = `closed-loop-demo-5`**
- worktree 实测位置：`<AO_DATA_DIR>/worktrees/<project>/<session>`
  （旧文档写的 `data/worktrees/...` 已修正）

## 真实事件采集（AOAdapter，动态端口）

worker conversation 实测活动（一轮）：
- 4 条 codex provider 重连错误（`Reconnecting... 2/5..5/5`，同指纹）
- 多条失败的 pytest 命令（`command` status=failed）
- worker 回复：`DONE_RUNNING`

## 触发 Alert（Observer 真实检测）

- SQLite `closed_loop.db` 记录 **alerts: 3**
- alert_type = `REPEATED_ERROR`，error_count >= 3（同指纹 codex provider 错误
  + 失败 pytest 命令）→ 超过 count=3 阈值
- project_id=`closed-loop-demo`，worker_id=`closed-loop-demo-5`

→ **REPEATED_ERROR_DETECTION: 真实数据验证通过**

## AuditResult（真实 Claude GLM-5.2 Auditor）

- **audits: 2**（两次 LOCAL_FIX 审计；第三次因 budget 耗尽判 HUMAN，已被 gate 路径覆盖）
- decision = `LOCAL_FIX`，confidence = 0.92
- evidence：git_diff（仅 `__pycache__` 变更，app.py 未改）+ test_failure
  （pytest ImportError: cannot import name 'divide'）
- diagnosis：worker 未实现 divide，反复跑失败测试；provider 重连为瞬态噪声
- recommended_action：在 app.py 实现 divide(a,b)，b==0 抛 ValueError

→ **Auditor 正确把 provider 重连噪声判为瞬态、把功能缺失判为 LOCAL_FIX**
  （修复了初版把 provider 错误误判为基础设施故障→HUMAN 的缺陷）

## PlannerAction（真实 Claude GLM-5.2 Planner）

- **planner_actions: 3**
- action = `SEND_LOCAL_FIX`，target_session_id = `closed-loop-demo-5`
- message：具体可执行——"在 app.py 实现 divide(a, b)，b==0 抛
  ValueError('division by zero')，否则返回 a/b；不要修改 tests/；
  完成后运行 pytest 验证"

## ActionExecutor（真实 ao send）

- **executed_actions: 3**（3 次 `ao send --session closed-loop-demo-5 --message <fix>`）
- idempotency：同一 action_id 仅执行一次
- shell-content guard：message 不含 `&&`/`|`/`rm`/`del`/`Remove-Item`

## Worker 实际修复

闭环驱动后，worker worktree 的 `app.py`：
```python
def add(a: float, b: float) -> float:
    return a + b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
```
git diff：`app.py | 6 ++++++`（仅 allowed_paths 内，tests/ 未动）。

## Integration Gate（真实 worktree 执行）

- **gate_runs: 2**
- 命令：`python -m pytest -q`，cwd = worker worktree
- **exit_code = 0**，stdout = `..  [100%]\n2 passed in 0.01s\n`
- forbidden_paths 门禁：忽略 `__pycache__/*.pyc`（pytest 运行产物），
  仅检查源码改动 → app.py 在 allowed_paths 内，无禁止路径违反

→ **GATE_PASS: 真实验证通过**

## 关键缺陷修复（本次实跑暴露并修复）

1. **Planner 动态端口解析**：`ao.run` 是 JSON `{"port": 3001, ...}`，
   旧代码 `line.split(":")[1].strip()` 得到 `"3001,"`（尾逗号）→
   `http://127.0.0.1:3001,` 请求失败 → "no parseable JSON reply"。
   修复：剥离非数字字符。
2. **状态机未接入控制流**：旧 `step()` 从 `TASK_READY` 直接到
   `TASK_READY→WORKER_RETRYING`（非法转换，被静默跳过），闭环永不推进。
   修复：完整接入 `TASK_READY→WORKER_RUNNING→AUDIT_PENDING→
   PLANNER_PENDING→LOCAL_FIX_PENDING→WORKER_RETRYING→GATE_PENDING→DONE`，
   并在 `WORKER_RETRYING` 等待 worker idle 后触发 Completion Gate。
3. **Auditor/Planner 子进程模型**：`claude -p` 继承 settings.json 的
   `ANTHROPIC_MODEL=deepseek-v4-pro`（已 403 / 无法结构化输出）。
   修复：子进程 env 覆盖 `ANTHROPIC_MODEL=GLM-5.2`。
4. **命令行长度限制**：npm shim `.cmd` 经 cmd.exe，argv ~8k 上限，
   大 bundle 作为 `-p` 参数静默空输出。修复：prompt 走 stdin，
   system-prompt 走 `--system-prompt-file`。
5. **`--json-schema` 结构化输出降级**：部分网关模型过不了 schema 重试
   （`error_max_structured_output_retries`）。修复：第二次尝试去掉
   `--json-schema`，prompt-only JSON + 围栏剥离。
6. **worktree 路径**：实测在 `<AO_DATA_DIR>/worktrees/<project>/<session>`，
   非旧文档的 `data/worktrees/...`。
7. **forbidden_paths 门禁误杀**：`tests/__pycache__/*.pyc` 是 pytest 产物，
   被当成"修改 tests/"。修复：忽略 `__pycache__`/`.pyc`。
8. **预算计数持久化**：local_fixes/replans 计数写 SQLite `counters` 表，
   进程重启不丢、不超发。
9. **TaskSpec 持久化**：写入 `tasks` 表。
10. **`pytest.ini`**：根目录 `pytest` 不再误收集 `demo/` 模板。

## 是否存在人工业务内容转述

**否**。全链路中：
- Alert 由 Observer 从真实 AO 事件自动检测；
- EvidenceBundle 由程序自动组装（TaskSpec + alert + events + worker_status
  + git_diff + failed_criteria + history）；
- AuditResult / PlannerAction 由真实 Claude(GLM-5.2) 生成并经 schema 验证；
- 修复指令由 Planner 生成、ActionExecutor 经 `ao send` 送入 worker；
- worker（codex）自主实现 divide；Gate 自主跑 pytest。
- 无任何 Auditor 结果、PlannerAction、修复说明由人工复制粘贴。

## 复现命令

```powershell
# 环境
$env:AO_DATA_DIR = "E:\智理杯智能体大赛\ao-data"
$env:AO_RUN_FILE = "E:\智理杯智能体大赛\ao-data\ao.run"
$ao = "E:\智理杯智能体大赛\ao-app\resources\daemon\ao.exe"
& $ao status

# 1. (一次) 建库 + 注册项目
cd "E:\智理杯智能体大赛\ao-supervision-sidecar"
.\scripts\setup_demo_repo.ps1

# 2. spawn worker 跑失败测试（制造 REPEATED_ERROR），自动绑定 session id
.\scripts\Run-ClosedLoopDemo.ps1   # spawn+绑定+--watch 到 DONE
# 或分步：
& $ao spawn --project closed-loop-demo --harness codex --name demo-worker `
  --mode chat --prompt "Read app.py and tests/test_divide.py. Then run 'python -m pytest -q' three times back-to-back. Do NOT modify code. Reply DONE_RUNNING when finished."
# 记下 session id，写入 --worker-session 或 task json

# 3. 真实闭环（真实 GLM-5.2 Auditor + Planner + ao send + Gate），跑到 DONE
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --worker-session <sid> --watch
```

## 测试

- `python -m pytest -q` -> **61 passed**（含 5 个 phase-3 闭环/gate 测试）
- 单元测试不调用真实 Claude/Codex/AO（用 Fake providers + 临时 SQLite）。

## 已知限制

- 真实 Auditor/Planner 单次调用 ~30–60s（GLM-5.2 经网关）；
  `--watch` 轮询间隔 10s。
- worker（codex）收到 LOCAL_FIX 后可能需多轮才实际改代码；
  本次 demo 中第 2 轮 LOCAL_FIX 后 worker 实现了 divide。
- AO daemon 偶发在长跑后无响应，重启 `ao start` 即可恢复。

---

# Phase-4 真实闭环证据（2026-08-29）：静默完成路径 + claude-code worker

**场景**：worker 一次做对任务（无告警、无 L0 错误）——此前这条路径下 loop 会永远停在
`WORKER_RUNNING`，永远到不了 DONE。本次实机验证暴露并修复了该 gap。

## 真实运行链路（TASK-VERIFY-001 / worker `closed-loop-demo-10`，claude-code harness）

```
TASK_READY -> WORKER_RUNNING            (observer: 真实 worker 事件)
           -> AUDIT_PENDING             (静默完成路径: worker idle → completion audit)
           -> PLANNER_PENDING           (真实 Auditor GLM-5.2: PASS, conf 0.95)
           -> GATE_PENDING              (真实 Planner GLM-5.2: CANDIDATE_DONE + plan)
           -> DONE                      (IntegrationGate: pytest 2 passed, exit=0)
```

命令：

```
python -m src.closed_loop_cli --task tasks/demo-verify.json \
  --worker-session closed-loop-demo-10 --once --db verify-run2.db \
  --instruct "目标：测试全绿即 DONE；禁止修改 tests/ 下任何文件"
```

## 关键证据（runtime/*.jsonl）

- **真实 Auditor（COMPLETION 模式）**：decision `PASS`，confidence `0.95`，
  证据键 `git_diff / test_output / criteria_report / budget_check`，
  failed_criteria 为空。
- **真实 Planner（领导型）**：`CANDIDATE_DONE`；`plan` 字段给出完整推理
  （divide 已实现含除零 ValueError、pytest 2 passed、tests/ 未改、__pycache__
  仅为字节码缓存、预算未耗尽）；`reason` 显式引用了用户指令
  （"符合用户指令：测试全绿即 DONE、未改 tests/"）——`--instruct` 被吸收进决策。
- **Integration Gate**：worktree
  `ao-data\worktrees\closed-loop-demo\closed-loop-demo-10` 上跑
  `python -m pytest -q` → **exit 0，`2 passed in 0.01s`**；
  路径门禁（`worktree.py.path_violations`）零违规
  （`.pytest_cache`/`__pycache__` 已按 artifact 过滤，不再误报）。

## 本次实机暴露并修复的 3 个缺陷

1. **静默完成 gap（P0）**：worker idle 且无 alert/L0 → 永停 WORKER_RUNNING。
   修复：`step()` 增加 `_maybe_idle_completion()`——idle + 无告警 + 无新错误
   → 一次 COMPLETION_AUDIT，受 `idle_audit_cooldown_seconds`（默认 300s）节流。
2. **Planner 输出 None 字段被 schema 拒绝（P0）**：真实 GLM-5.2 Planner 常输出
   `"message": null` / 省略 `plan`，schema 要求 string → 校验失败 → 降级 HUMAN
   （一个健康的 PASS 决策被误杀）。修复：`_coerce_planner_strings()` 在校验前
   将 None→""、非字符串→JSON 序列化。
3. **gate 误报 `.pytest_cache`（P1）**：`worktree._is_artifact` 原来只过滤
   `__pycache__`/`.pyc`；pytest 运行必然产生的 `.pytest_cache` 会被路径门禁
   误判为"越界修改"。修复：artifact 标记扩展到
   `.pytest_cache/.mypy_cache/.ruff_cache/.coverage/.tox/.hypothesis/.eggs`。

## 测试

- `python -m pytest -q` → **76 passed**（新增 3 个静默完成路径测试）

## 复现要点

- `--db <name>` 参数：在 `runtime/` 下用新 StateStore 文件做干净运行，
  不重放已 processed 的事件。
- 跨天残留的 `started_at` 计数器会触发 `max_runtime_seconds` 看门狗 → HUMAN
  （这是预算机制的正确行为，不是 bug）；干净验证用新 `--db`。

