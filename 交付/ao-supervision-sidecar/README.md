# ao-supervision-sidecar

V0.1 闭环：在 TASK-AO-02 的只读监督侧车基础上，扩展为
**Codex Worker 重复失败 → Observer 检测 → 只读 Auditor → 唯一 Planner →
ao send 回 Worker → Worker 修复 → Integration Gate → DONE** 的自动闭环。

| 告警 | 规则 | 默认阈值（config/default.yaml） |
|---|---|---|
| `REPEATED_ERROR` | 同一 (project, worker, 错误指纹) 在时间窗内出现 ≥ N 次 | 600s 内 ≥ 3 次 |
| `NO_PROGRESS` | 时间窗内活动事件 ≥ N 且 strong 进展事件 ≤ M | 900s 内 ≥ 8 活动 & 0 strong 进展 |

**V0.1 架构（唯一，无多余角色）**：1 AO Planner/Orchestrator + 1 Codex Worker
+ 1 只读 Auditor + 1 Deterministic Observer + 1 Integration Gate。
详见 `docs/V01_ARCHITECTURE.md`。不接入 GLM/Kimi/第二 Worker/Dashboard。

## 目录

```
docs/AO_INTEGRATION_AUDIT.md   Phase 1：AO 只读接口审计（实测，含 FAILED 条目）
schemas/event.schema.json      统一事件 JSON Schema
schemas/alert.schema.json      告警 JSON Schema
config/default.yaml            全部阈值（规则代码不硬编码任何数值）
src/ao_adapter.py              AO REST/SSE 只读适配（含非法 JSON 修复）
src/event_normalizer.py        AO 原始数据 -> NormalizedEvent（唯一认识 AO 字段的层）
src/fingerprints.py            错误指纹归一化（时间戳/ID/路径/行号漂移不敏感）
src/observer.py                两条确定性规则（读取 config 阈值）
src/models.py                   数据模型（与 schema 字段一致）
src/cli.py                      入口：--once / --watch / --fresh / --project / --use-sse
tests/                          25 个测试，含样本 A/B/C
runtime/events.jsonl           实际运行输出（追加写）
runtime/alerts.jsonl           实际运行输出（追加写）
runtime/events.example.jsonl   样例
runtime/alerts.example.jsonl   样例
```

## 快速开始

### 监督侧车（只读，TASK-AO-02）

```bash
cd ao-supervision-sidecar
python -m src.cli --once --fresh     # 单次快照：拉取 -> 归一化 -> 规则 -> 落盘
python -m src.cli --watch            # 每 poll_interval_seconds 轮询一次，Ctrl+C 停止
python -m src.cli --watch --use-sse  # 轮询 + SSE 实时流（SSE 空闲无心跳，自动重连续传）
python -m pytest tests/ -q           # 全部 61 个测试
```

### V0.1 闭环（TASK-V01）

```bash
# 1. 建 demo 仓库 + 注册项目（一次性，幂等）
./scripts/setup_demo_repo.ps1
# 2. spawn worker 跑失败测试 3 次（制造 REPEATED_ERROR），见 docs/V01_OPERATOR_GUIDE.md
# 3. dry-run 验证链路
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --dry-run
# 4. 真实跑（Claude Auditor + AO Planner + ao send + Gate）
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --once
# 或持续监督
./scripts/Start-ClosedLoopV01.ps1 -Task tasks/demo-repeated-error.json
```

详见 `docs/V01_OPERATOR_GUIDE.md`、`docs/V01_ARCHITECTURE.md`、
`docs/V01_REAL_AO_EVIDENCE.md`。

## 事件归一化规则（确定性映射，见 event_normalizer.py）

| AO 原始 | 归一化后 |
|---|---|
| 首次见到 session | `worker_started` |
| session `activity.state` 变化 | `task_state_changed` |
| session `isTerminated` | `worker_finished` |
| activity `error` | `error`（activity=True；message 做指纹） |
| activity `command` / `mcp_tool` completed | `command_executed`（activity=True） |
| activity `command` / `mcp_tool` **failed** | `error`（activity=True；摘要做指纹 —— 失败的测试运行可触发 REPEATED_ERROR） |
| activity `approval` | `task_state_changed`（activity=True） |
| activity `file_change` completed | `file_changed`（**progress=True**） |
| activity `file_change` failed | `error`（activity=True；摘要做指纹） |
| activity `reasoning` | 丢弃（思考不算活动） |
| turn 完成且 `diff.files` 非空 | `file_changed`（progress=True，每 turn 一条） |

AO 的 activities **没有独立时间戳**：事件时间取 turn `requestedAt` →
worker `lastActivityAt` → 当前时间（审计文档 §3）。

## 告警判定（observer.py）

- 每条事件按 `event_id` 去重（重轮询同一 AO 条目不会重复计数）；窗口锚定在
  该 worker 最新事件时间。
- 告警按 (project, worker, 规则, 指纹) + `cooldown_seconds` 抑制重复。
- 阈值全部来自 `config/default.yaml`，代码中无硬编码数值。

## 已知限制

- AO 无 WebSocket；SSE 无按项目过滤、空闲无心跳（adapter 自动重连 +
  `Last-Event-ID` 续传）。
- 部分端点返回非法 JSON（孤立反斜杠 / `\'`）与中文乱码 —— 由
  `ao_adapter.repair_json` 与「路径取列表端点」规避。
- 已终止 session 的 conversation 端点返回 HTTP 409（adapter 自动跳过）。
- 失败的 `pytest` 在 AO 中记录为 `command` 活动（status=failed）——
  归一化层已将其映射为 `error` 事件（真实联调确认）。

## 联调记录（2026-08-27，真实 AO）

1. 对 `ao-smoke-test` 全部 session 运行 `--once --fresh`：
   - 收集 32 条真实事件（worker_started / file_changed / error / command /
     approval / worker_finished…）写入 `runtime/events.jsonl`；
   - **真实告警**：`ao-smoke-test-3` 的 4 条 codex provider 错误
     （Reconnecting…，同指纹）在 10 分钟窗内 ≥3 → `REPEATED_ERROR` 正确触发；
   - NO_PROGRESS 未误触发（各 worker 有真实进展或活动数未达阈值）。
2. 生成 `ao-smoke-test-4`（codex，chat 模式）：读 app.py/README.md +
   连续 3 次运行故意失败的 `pytest tests -q`（`tests/test_fail.py`，提交
   3c869c7）—— 真实运行确认：
   - 失败的测试运行在 AO 中记录为 `command` 活动 status=failed，归一化为
     `error` 事件（已进入 events.jsonl）；
   - 该 worker 的 codex provider 重连错误（同指纹 4 条）→ 真实
     `REPEATED_ERROR` 告警再次正确触发。
3. 样本 A/B/C 的确定性行为由 `tests/`（27 个测试，全部通过）覆盖。
4. `tools_probe.py / tools_evidence.py / tools_evidence2.py` 为审计探针
   （采集本 README 与审计文档所用证据，可删除）。
