# V0.1 运维指南

## 前置

1. AO daemon 在跑（`ao status` -> ready），端口由 `ao.run` 记录。
2. 环境变量（每次新终端）：
   ```powershell
   $env:AO_DATA_DIR = "E:\智理杯智能体大赛\ao-data"
   $env:AO_RUN_FILE = "E:\智理杯智能体大赛\ao-data\ao.run"
   ```
3. Codex / Claude Code 均 authorized（`ao agent ls`）。

## 一次完整 Demo

```powershell
cd "E:\智理杯智能体大赛\ao-supervision-sidecar"
# 1. 建 demo 仓库 + 注册项目（一次性，幂等）
.\scripts\setup_demo_repo.ps1
# 2. spawn worker 跑失败测试 3 次
$ao = "E:\智理杯智能体大赛\ao-app\resources\daemon\ao.exe"
& $ao spawn --project closed-loop-demo --harness codex --name demo-worker --mode chat --prompt "Read app.py and tests/test_divide.py. Then run 'python -m pytest -q' three times back-to-back. Do NOT modify code. Reply DONE_RUNNING when finished."
# 3. 等 ~90s，把返回的 session id 写进 tasks/demo-repeated-error.json 的 worker_session_id
# 4. dry-run 验证链路
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --dry-run
# 5. 真实跑（Claude Auditor + AO Planner + ao send + Gate）
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --once
```

## 持续监督

```powershell
.\scripts\Start-ClosedLoopV01.ps1 -Task tasks\demo-repeated-error.json   # 前台 watch
.\scripts\Stop-ClosedLoopV01.ps1                                          # 仅停 closed_loop_cli，不动 AO
```

## 产物（runtime/）

- `closed_loop.db` — SQLite 持久状态（8 表，跨重启去重）
- `events.jsonl` / `alerts.jsonl` — Observer 产物
- `audits.jsonl` / `planner_actions.jsonl` / `state_transitions.jsonl` / `gate_runs.jsonl`

## 调整阈值/预算

- 观察阈值：`config/default.yaml`
- 任务预算（max_local_fixes 等）：`tasks/demo-repeated-error.json` 的 budgets
- Auditor 预算：`src/auditor.py` `ClaudeCliAuditorProvider(budget_usd=...)`

## 排障

- **`ConnectionRefused` / 端口错**：确认 `AO_RUN_FILE` 指向有效 `ao.run`；
  adapter 从该文件读端口。
- **Codex spawn 报 `wire_api` 不支持**：确认 `.codex/config.toml` 中 GLM 块
  `wire_api = "responses"`（非 "chat"）。
- **Auditor 返回 HUMAN**：看 `audits.jsonl` 诊断；通常是 Claude 输出非合法
  JSON 两次（检查 schema 或重试）。
- **Gate 失败循环**：受 `max_local_fixes`/`max_replans` 限制，超限转 HUMAN。
