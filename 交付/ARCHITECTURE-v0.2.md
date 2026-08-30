# 闭环多智能体系统 v0.2 整合架构基线

> 状态：待搭建 · 2026-08-30
> 上游来源：CL-AO v0.1（组员，严谨控制层）+ ao-supervision-sidecar（Claude，mission 产品化能力）
> 本文档是后端搭建的唯一权威基线；任何偏离本文档的修改需征得项目负责人同意。

## 一、角色定编（最终）

| 角色 | 数量 | 本质 | 职责 |
|---|---|---|---|
| Planner | 1 | LLM agent（AO orchestrator 会话） | 理解用户目标、拆解 mission、派发 Worker、裁决升级、维护 `memory.md` 与 `project.md` |
| Supervisor 监督台 | 1 套 | **Observer（确定性程序）+ Auditor（LLM agent，只读）** | 监督 Worker、卡住时介入纠错、预判/发现大问题后提交 Planner 裁决 |
| Verifier | 1 | LLM agent + Gate（确定性程序） | PV 任务验证、独立验收、反作弊检查、项目级集成测试 |
| Worker | ≥2 | LLM agent（AO worker 会话） | 边界清晰的编码任务、提交验证证据、受阻时可直接上报 Planner |

**模型策略**：不使用 Codex。Worker 默认 claude-code（GLM-5.2 已实机验证）；Planner/Auditor/Verifier 可用 Claude / GLM / Kimi，配置于 `config/default.yaml` 的 `worker.model` / `roles.*.model`。

### 1.1 监督台内部职责划分（关键决策）

- **Observer（程序，非 agent）**：固定间隔只读轮询 AO 公开 REST；确定性识别 `REPEATED_FAILURE` / `STALL` / `MILESTONE` / 预算阈值与预判性风险信号；输出 trigger + evidence，不作语义决策。继承 CL-AO `observer.py` 的进展签名与 fingerprint 纪律。
- **Auditor（agent，只读）**：接收 Observer 证据包或 Worker 主动上报，做语义审计；只允许输出 `PASS / LOCAL_FIX / REPLAN / HUMAN / ESCALATE` 五类结论；`LOCAL_FIX` 直接返回 Worker（L0 局部闭环），其余进入 Planner 裁决（L1）。只读性 = 提示契约 + 执行前后公开 REST 状态核验（沿用 CL-AO 纪律）。

不合并为纯 agent 的原因：心跳式检测必须零模型成本、零幻觉；语义判断才用 LLM。

## 二、全连接循环通道（Loop Bus）

所有 agent 之间的通道敞开、可循环工作，由一个**确定性消息总线（Loop Bus，普通程序）**承载。Bus 是唯一接触 AO 传输层的组件，agent 之间不直接互发。

### 2.1 消息信封（Envelope）

```json
{
  "msgId": "uuid",
  "threadId": "mission-or-issue 关联 ID",
  "from": "planner|auditor|verifier|worker:<id>|observer|gate|human",
  "to":   "同上",
  "kind": "见 2.2",
  "payload": {},
  "idempotencyKey": "由 threadId+from+阶段确定性生成",
  "hop": 0
}
```

传输纪律完全继承 CL-AO：`clientMessageId` 幂等、Duplicate 恢复只接受 `role=user`/`origin=human`/正文严格相等/`turnId` 非空的唯一消息、其余 fail closed。

### 2.2 两两通道矩阵（精确方向，项目负责人已裁决）

规则：**Auditor→Verifier 单向、Observer→Verifier 单向，其余两两之间全部双向**。Observer 与 Gate 是程序：它们对外只发信号，收到的只能是"指令"类消息。

| 通道对 | 方向 | 消息类型 |
|---|---|---|
| Planner ↔ Worker | **双向** | P→W：`TASK_DISPATCH` / `LOCAL_FIX` / `REPLAN_DISPATCH`；W→P：`BLOCKER_REPORT` / `CHECKER_REQUEST` |
| Planner ↔ Auditor | **双向** | P→A：`AUDIT_REQUEST`；A→P：`AUDIT_REPORT` / `ESCALATION` |
| Planner ↔ Verifier | **双向** | P→V：`PV_TASK`；V→P：`PV_RESULT` / `VERDICT` |
| Planner ↔ Observer | **双向** | P→O：`WATCH_DIRECTIVE`（调整观察焦点/阈值）；O→P：`RISK_SIGNAL` |
| Planner ↔ Gate | **双向** | P→G：`GATE_RUN`；G→P：`GATE_EVIDENCE` |
| Worker ↔ Auditor | **双向** | W→A：`STATUS_CLAIM`；A→W：`LOCAL_FIX` / `AUDIT_QUERY` |
| Worker ↔ Verifier | **双向** | W→V：`VERIFY_REQUEST`；V→W：`FIX_REQUEST` |
| Worker ↔ Observer | **双向** | W→O：`STATUS_NOTE`；O→W：`STALL_NOTICE`（停滞提醒，非指令） |
| Auditor ↔ Observer | **双向** | A→O：`FOCUS_WATCH`（要求定向观察）；O→A：`TRIGGER` |
| Auditor → Verifier | **单向** | A→V：`AUDIT_VERIFY_REQUEST`（请求硬性验证证据；结果只能经 Verifier→Planner 回流，Auditor 不直接收） |
| Observer → Verifier | **单向** | O→V：`TRIGGER_VERIFY`（里程碑等触发验证；Verifier 不回传 Observer） |
| 任意 → Human | 单向 | `HUMAN`（人工兜底） |
| Planner → 用户 | 单向 | `FINAL_REPORT`（**所有最终结果由 Planner 总结后统一报告用户**） |
| 用户 → 任意 agent | 单向直发 | `USER_DIRECTIVE`（前端输入框直发任何 agent） |
| 用户 → Planner（镜像） | 自动 | `USER_DIRECTIVE_COPY`（见下方可见性规则） |

**用户指令可见性规则（2026-08-30 裁决）**：用户可以在输入框中对任何一个 agent 直接下发指令或修改命令。发给 **Planner 的任务安排是私密的**，只有 Planner 能看到；发给**其他任何 agent 的指令，Bus 自动镜像一份 `USER_DIRECTIVE_COPY` 给 Planner**——即用户的一切指令 Planner 必然可见。该规则在后端由 Loop Bus 强制（路由表 + 镜像逻辑），在前端由输入框的目标选择器和"Planner 可见"标识体现。

设计理由：Auditor→Verifier 与 Observer→Verifier 保持单向，是为了让"验证结论的出口"唯一收敛到 Planner——Verifier 的任何结果都经 `PV_RESULT`/`VERDICT` 回 Planner，再由 Planner 决定是否转发 Auditor，避免监督方与验证方绕过裁决层直接对循环。

### 2.3 双通道提交与去重（重点需求）

Worker 受阻可直接报 Planner，监督台发现同一问题也可同时报 Planner——两渠道**都保持开放**。去重由 Bus 完成：

- 每份上报计算 `issueFingerprint`（任务 ID + 规范化错误指纹 + 证据来源命名空间，沿用 CL-AO `normalize_failure_fingerprint` 纪律）；
- 相同 fingerprint 的后续上报**不重复触发裁决**，只追加证据并回执"已并入既有裁决 thread"；
- Planner 对每个 thread 只裁决一次，裁决结果广播给所有上报方。

### 2.4 有界循环（防死循环）

每个 `threadId` 携带 hop 计数与预算（最大审计次数、最大 LOCAL_FIX 次数、总时间上限，默认继承 CL-AO：max-audits 3、overall-timeout 600s）；超限或检测到进展签名停滞 → 确定性转 `HUMAN`。Bus 不建立后台服务，前台有界运行。

### 2.5 监督节奏（用户可精确调控）

所有时间参数集中在 `config/default.yaml`，**用户输入多久就是多久**，程序不做二次取整或下限钳制（仅要求为正数）：

| 参数 | 推荐默认值 | 含义 |
|---|---|---|
| `observer.interval_seconds` | **10** | Observer 轮询间隔 |
| `auditor.audit_interval_seconds` | **300**（5 分钟） | 无触发时的例行审计节奏；有 TRIGGER 时立即审计，不受此间隔限制 |
| `observer.stall_threshold_seconds` | 300 | 停滞判定阈值 |
| `observer.failure_threshold` | 2 | 重复失败判定次数 |
| `bus.max_audits_per_thread` | 3 | 单 thread 最大审计轮数 |
| `bus.overall_timeout_seconds` | 600 | 单 thread 总时间上限 |

### 2.6 预判机制（诚实边界）

"提前预判错误"由三层实现，**能系统性提前拦截一大类问题，但不承诺预知所有错误**：

1. **确定性预警（Observer，程序）**：错误指纹首次出现即预警（不等到达阈值）、接近预算 80% 预警、修改越出 `allowed_paths` 边界、长时间无 commit；
2. **派发前预检（Auditor，agent）**：Planner 派发前，Auditor 对任务拆解做语义预检（路径冲突、依赖环、验收条件不可测、范围漂移），不通过则退回 Planner 重拆；
3. **记忆模式匹配（Planner + memory.md）**：历史失败模式入库，新任务命中相似模式时自动附加约束。

### 2.3 双通道提交与去重（重点需求）

Worker 受阻可直接报 Planner，监督台发现同一问题也可同时报 Planner——两渠道**都保持开放**。去重由 Bus 完成：

- 每份上报计算 `issueFingerprint`（任务 ID + 规范化错误指纹 + 证据来源命名空间，沿用 CL-AO `normalize_failure_fingerprint` 纪律）；
- 相同 fingerprint 的后续上报**不重复触发裁决**，只追加证据并回执"已并入既有裁决 thread"；
- Planner 对每个 thread 只裁决一次，裁决结果广播给所有上报方。

### 2.4 有界循环（防死循环）

每个 `threadId` 携带 hop 计数与预算（最大审计次数、最大 LOCAL_FIX 次数、总时间上限，默认继承 CL-AO：max-audits 3、overall-timeout 600s）；超限或检测到进展签名停滞 → 确定性转 `HUMAN`。Bus 不建立后台服务，前台有界运行。

## 三、项目记忆文件（Planner 维护）

系统工作期间至少维护两个工作文件，位于目标项目根目录：

- **`project.md`** — 重大事项与进展记录：mission 拆解、每次裁决结论、Worker DONE、Gate/Verifier 结果、人工介入记录。
- **`memory.md`** — 项目记忆：任务上下文、已确认的事实、历史失败模式、复用决策。

写入纪律：Planner 在每轮裁决输出中携带 `memoryUpdates` / `projectUpdates` 条目；**Bus 负责原子落盘**（Planner 生成内容、程序保证写入，双保险不丢失）。两个文件随 mission 归档。

## 四、两级闭环（保留 CL-AO 分层）

- **L0 局部闭环**：明确归属当前 Worker 的问题（编译/测试失败、审批卡死等）→ Auditor `LOCAL_FIX` 直接回 Worker，不升级。
- **L1 项目级闭环**：停滞、偏航、根因错误、跨任务集成问题 → Observer/Auditor/Worker 任一渠道 → Planner 裁决 → 重新派发 → 再验证。
- **终局**：全部子任务 DONE 后必须过 Integration Gate（clean checkout、显式 argv、`shell=False`、确定性证据）+ Verifier 独立验收，失败证据自动回流 Auditor/Planner/Worker。

## 五、从两条既有代码线的取舍

| 能力 | 来源 | 处置 |
|---|---|---|
| AO Client（daemon 发现、OpenAPI 校验、Conversation 读写、幂等恢复） | CL-AO `ao_client.py` | **直接继承**，扩展 approvals resolve |
| 严格协议（AuditRequest/Report/PlannerDecision 四决策） | CL-AO `protocol.py` | **继承并扩展** Envelope 与 PV/CHECKER 类型 |
| Deterministic Observer | CL-AO `observer.py` | **继承**，合并 sidecar 的 NO_PROGRESS 规则与指纹归一化 |
| Integration Gate | CL-AO `integration_gate.py` | **继承**（clean checkout、显式 argv、稳定 evidence） |
| Mission 拆解与状态机 | sidecar `mission.py` / `mission_cli.py` | **移植**，状态机重写为 Bus 驱动 |
| 受限自动审批（allowed_paths + 安全命令白名单） | sidecar `closed_loop.py` / `ao_adapter.resolve_approval` | **移植**，审批策略入 `config/default.yaml` |
| 双 Worker 并行 + worktree 隔离合并 | sidecar `worktree.py` | **移植**，支持 ≥2 Worker |
| 独立 Verifier + anti-gaming | sidecar `verifier.py` + prompts | **移植** |
| SQLite 状态存储（重启水合恢复） | sidecar `state_store.py` | **移植**，但与 CL-AO 幂等恢复并存：重启先查 AO Snapshot，DB 只作本地索引 |
| 309 项离线测试纪律 | CL-AO | **延续**，新模块同标准 |

## 六、不变量（不可破坏）

1. 项目级控制权只属于唯一 Planner；
2. Observer/Gate 是程序不是 agent，语义判断才用 LLM；
3. Auditor 只读；Verifier 不改实现代码；
4. 所有通道消息幂等、可恢复、fail closed；
5. 每个 thread 有界，超限转 HUMAN；
6. Worker 自报完成不单独作为完成依据；
7. 不使用 Codex。
