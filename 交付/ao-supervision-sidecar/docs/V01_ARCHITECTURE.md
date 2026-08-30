# V0.1 闭环架构

## 实体（唯一架构，无多余角色）

```
1 个 AO Planner / Orchestrator   (ao spawn --kind orchestrator, claude-code)
1 个按任务创建的 Codex Worker     (ao spawn --kind worker, codex)
1 个只读 Auditor                 (claude -p, --disallowedTools "*")
1 个 Deterministic Observer      (src/observer.py)
1 个普通程序 Integration Gate    (src/integration_gate.py)
```

固定 4 个协议：TaskSpec / AuditResult / PlannerAction / ProjectState（schemas/）。

## 数据流

```
AO daemon (REST/SSE)
   |
   v
AOAdapter (src/ao_adapter.py)  -- 修复非法 JSON, 动态端口(从 ao.run 读)
   v
EventNormalizer (src/event_normalizer.py)  -- AO 原始 -> NormalizedEvent
   |  progress_strength: none|weak|strong
   v
Observer (src/observer.py)  -- REPEATED_ERROR / NO_PROGRESS (只用 strong)
   |  (SQLite 去重: event_id / alert_id 跨重启不重复)
   v
[新 Alert] -> EvidenceBundle -> Auditor (src/auditor.py)
   |  只读: claude -p --disallowedTools "*" --json-schema
   v
AuditResult (PASS|LOCAL_FIX|REPLAN|HUMAN)
   |  (schema 验证; 失败重试1次 -> HUMAN)
   v
Planner (src/planner_adapter.py)  -- AO orchestrator session + ao send
   v
PlannerAction (CONTINUE|SEND_LOCAL_FIX|REPLAN_SPAWN|CANDIDATE_DONE|HUMAN)
   |  (schema 验证; 幂等: 同 action_id 只执行一次; 预算: max_local_fixes/max_replans)
   v
ActionExecutor (src/action_executor.py)  -- 固定映射, ao send / ao spawn
   v
Worker 重试 / 完成
   v
[CANDIDATE_DONE] -> IntegrationGate (worktree 内跑 TaskSpec.gate_commands)
   v
Gate pass -> DONE ; Gate fail -> 重新 Audit (受预算限制)
```

## 状态机（src/contracts.py LEGAL_TRANSITIONS）

```
TASK_READY -> WORKER_RUNNING
WORKER_RUNNING -> AUDIT_PENDING | GATE_PENDING | HUMAN | FAILED
AUDIT_PENDING -> PLANNER_PENDING | HUMAN
PLANNER_PENDING -> LOCAL_FIX_PENDING | REPLAN_PENDING | GATE_PENDING | WORKER_RUNNING | HUMAN
LOCAL_FIX_PENDING -> WORKER_RETRYING | HUMAN
WORKER_RETRYING -> AUDIT_PENDING | GATE_PENDING | HUMAN
REPLAN_PENDING -> WORKER_RUNNING | HUMAN | FAILED
GATE_PENDING -> DONE | AUDIT_PENDING | HUMAN | FAILED
DONE / HUMAN / FAILED : 终态
```

## 持久化

`runtime/closed_loop.db` (SQLite, src/state_store.py)，8 张表：
tasks / state_transitions / processed_events / alerts / audits /
planner_actions / executed_actions / gate_runs。进程重启后从游标继续，
不重复处理历史事件、不重复触发审计/规划/执行。

## 边界（严格遵守）

- Observer 不做语义判断；Auditor 只读不改代码不控制 Worker；
  Planner 唯一控制权但不直接改代码/跑测试/宣布 DONE；ActionExecutor 是普通程序
  只执行固定映射；Worker 不改验收标准。
- 不接入 GLM/Kimi/第二 Worker/Dashboard。
- 不自动合并、不删分支、不改 TaskSpec/tests、不绕过预算。
- 不使用 --dangerously-skip-permissions。
