# Mission 闭环实机证据（2026-08-29）

**状态：VERIFIED** — 1 个领导型 Planner 分解 mission → 2 个并行 claude-code worker →
监督（Auditor）→ 独立验证（Verifier）→ 集成合并 → 终局 gate + verifier。

## 目标架构（用户需求，全部落地）

> 一次完整用户指令 → 其余全部自动完成：
> 1 Planner 操控全局 + ≥2 worker + 1 监督 agent（Observer+Auditor）+ 1 独立验证 agent（Verifier）。

## 真实运行（mission-demo4.db，全部 GLM-5.2 实机）

Mission：`tasks/mission-demo.json`（MISSION-DEMO-001）——"app.py 实现 divide；
新建 math2.py 实现 multiply + 测试"。全量 gate：`python -m pytest -q`。

```
用户指令（一次输入）
  └─ Planner.plan_decompose (GLM-5.2, mission-plan schema)
       S1: divide  @app.py,tests/test_divide.py   gate: pytest tests/test_divide.py
       S2: multiply @math2.py,tests/test_multiply.py gate: pytest tests/test_multiply.py
       （路径不相交、无依赖、各自子任务级 gate —— 规划正确处理了隔离 worktree 看不到兄弟产出的问题）
  ├─ S1 worker closed-loop-demo-18（claude-code, --model GLM-5.2）
  │    12:59:49 WORKER_RUNNING（派发即冻结 base，自动放行 allowed_paths 内的编辑）
  │    12:59:59 AUDIT_PENDING  （静默完成路径：idle → completion audit）
  │    13:00:30 PLANNER_PENDING（真实 Auditor PASS conf 0.93）
  │    13:01:13 GATE_PENDING   （真实 Planner CANDIDATE_DONE）
  │    13:01:14 VERIFIER_PENDING（子任务 gate: 2 passed）
  │    13:02:45 DONE           （独立 Verifier PASS：实现真实、未改 tests/）
  ├─ S2 worker closed-loop-demo-19
  │    12:59:49 WORKER_RUNNING
  │    13:02:56 AUDIT_PENDING  （NO_PROGRESS 告警 → 真实 Auditor LOCAL_FIX conf 0.92：
  │                              "写卡在 pending 审批上" → 精准诊断）
  │    13:04:41 WORKER_RETRYING（真实 Planner 发 LOCAL_FIX 指令，worker 恢复）
  │    13:18:31 …13:31:34 修复链重走 audit→planner→gate（2 passed）
  │    13:32:24 DONE           （独立 Verifier PASS）
  ├─ 集成 worktree integration-MISSION-DEMO-001
  │    git log: 3d420e7(S1) + b6503e2(S2) → merge ac2a738
  └─ 终局验证：全量 gate `python -m pytest -q` → 4 passed（2 divide + 2 multiply）
       mission-level Verifier → PASS（全部 4 条 AC 确认、anti-gaming 干净）
```

（时间戳为 UTC；S2 一次 verifier FAIL 是 untracked-file diff 缺陷，修复后重验 PASS，见下。）

## 终局工件（可复查）

- `ao-data/worktrees/closed-loop-demo/integration-MISSION-DEMO-001/`
  - `app.py`：add + divide（含除零 ValueError）
  - `math2.py`：multiply；`tests/test_multiply.py`
  - 全量 pytest：**4 passed**
- `runtime/mission-demo4.db`：状态转移 12 条（S1×7, S2×9 轮次）、audits、
  verifications（S1 PASS / S2 一次 FAIL 后 PASS）、gate_runs
- `runtime/*.jsonl`：alerts / audits / planner_actions / verifications / state_transitions

## 本次实机暴露并修复的 8 个缺陷

1. **`ao spawn` 缺 `--model GLM-5.2`（P0）**：worker 会用网关默认模型（403）。
   修复：`config/default.yaml worker.model` + `ActionExecutor._spawn_args`（初始
   spawn 与 replan 两处）。
2. **L0 nudge 打断在飞 turn（P0）**：worker 启动 23s 后就被 nudge，AO 拒绝
   （"ACP conversation already has a turn in flight"）甚至杀死 controller。
   修复：nudge 仅在 worker idle 时发送 + 300s 孵化宽限（`hatched_at` 计数器，
   独立于看门狗的 `started_at`）。
3. **审批无人应答（P0，用户批准的受限自动放行）**：claude-code worker 的每次
   Edit/Write/命令都要权限批准，无人值守 mission 必卡死（实测 30 分钟停滞）。
   修复：`AOAdapter.resolve_approval`（REST `POST …/approvals/{requestId}/resolve`，
   body `{"decisionId":"allow"}`）+ `ClosedLoop._maybe_auto_approve`：
   - 文件编辑：仅 `allowed_paths` 内 → allow_once（tests/、forbidden、越界一律留人工）
   - 命令：仅本任务 gate 命令 / pytest / git 只读+提交类 → allow_once
   实测：S2 越界请求改 `app.py` 被正确拒绝；`pytest tests/test_multiply.py -q`
   （gate 本身）被正确放行。
4. **子任务 gate 与隔离 worktree 矛盾（P0）**：每个 worker 在隔离 worktree 看不到
   兄弟产出，全量 gate 必失败。修复：`SubtaskPlan.gate_commands`（schema+prompt+
   controller 落地），全量 gate 留到集成树终局执行。
5. **frozen base 竞态（P0）**：base 此前在首次 gate/audit 时才冻结——worker 中途
   `git commit` 后冻结 = diff 永远为空（S1 实现了 divide 且已提交，verifier 却判
   "无源码改动"）。修复：**派发 worker 时立即冻结**（`_dispatch_ready`）。
6. **diff 看不见 untracked 新文件（P0）**：S2 的 math2.py/test_multiply.py 是新
   文件，`git diff` 根本不显示 → verifier 看到 "空 diff 与 changed_paths 矛盾"。
   修复：`git_diff_text` 补 `git add -N`（intent-to-add）后再 diff。
7. **VERIFIER_PENDING/审计死胡同（P1）**：verifier FAIL → AUDIT_PENDING 后无事件
   可触发重审（idle-completion 只认 WORKER_RUNNING）。修复：允许从
   AUDIT_PENDING 触发 completion audit；WORKER_RETRYING 分支同样能自动放行审批。
8. **mission 不随子任务终态（P1）**：任一子任务 HUMAN/FAILED 时 mission 悬空、
   watch 永转。修复：mission 跟随进入 HUMAN/FAILED 终态；watch 退出。
   另：终局 verifier 一次 claude 子进程瞬时故障即 FAIL 全 mission —— 修复为
   invalid-output 时延迟重试一次（本次实机即因此误判，重验为 PASS）。

（另有两个小修：`mission_cli` 重启后从 store 恢复 plan/tasks（不重新分解、不孤儿化
已派发 worker）；daemon 短暂无响应时 `_collect_all_events` 空快照重试而非崩溃。）

## 监督链的行为证据（非摆设）

- S2 worker 卡审批 → Observer NO_PROGRESS 告警 → **真实 Auditor 诊断**
  "写卡在 pending、worker 转入 waiting_input"（conf 0.92）→ **真实 Planner**
  发出 LOCAL_FIX（"恢复/重写 test_multiply.py、用 from math2 import multiply"）→
  worker 按指令修复 → 后续 gate 通过。领导型 Planner 的"发现问题→调整→再派发"
  闭环在实机上成立。
- **Verifier 拦截真问题**：S1 gate 显示 "2 passed" 但 diff 为空（base 竞态 +
  untracked 两个缺陷叠加），verifier 坚持判 FAIL（"按负标准 FAIL"）——正是设计
  要求的"验证对错能力"。
- **策略拒绝越界**：S2 worker 试图改 S1 的 `app.py`，自动放行策略正确拒绝。

## 复现命令

```powershell
cd "E:\智理杯智能体大赛\ao-supervision-sidecar"
$env:AO_DATA_DIR = "E:\智理杯智能体大赛\ao-data"
$env:AO_RUN_FILE = "E:\智理杯智能体大赛\ao-data\ao.run"
python -m src.mission_cli --mission tasks/mission-demo.json --watch --db <新名字>.db
```

## 测试

- `python -m pytest -q` → **95 passed**（新增 mission 终态跟随、受限自动放行、
  L0 宽限等测试；全 suite 为 fake providers + 临时 SQLite/真实临时 git 仓库）。
