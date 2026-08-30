# PLANS — 后续计划

## 当前（V0.1）状态
- Phase 0-3 完成并 commit；61 测试通过。
- 真实 AO 事件 → REPEATED_ERROR → Audit → Planner(SEND_LOCAL_FIX) 链路在
  dry-run 上端到端验证通过。
- 阻塞：真实 Claude Auditor / AO Planner 一次性实跑被 harness 分类器持续
  不可用阻断。

## P1 解除阻塞后立即做
1. `python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --once`
   真实跑通：Claude Auditor 输出 LOCAL_FIX → AO Planner SEND_LOCAL_FIX →
   `ao send` 发到 worker → worker 实现 divide → Gate 跑 pytest 通过 → DONE。
2. 把真实 audit_id / action_id / commit / gate exit code 写入
   `docs/V01_REAL_AO_EVIDENCE.md`。

## P2 增强（V0.2 候选，本次不做）
- 并行多 Worker（放开 MAX_PARALLEL_WORKERS）。
- Planner REPLAN_SPAWN 真实重规划闭环验证。
- 人类在回路 UI（仅观察，不复制业务内容）。
- 比赛材料整理。

## 明确不做（任务边界）
GLM/Kimi 接入、第二个 Planner、Coordinator/Risk/Reviewer 常驻、
Dashboard、云端/多仓库/长期记忆、自动合并/部署。
