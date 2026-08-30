# closed-loop-v2

闭环多智能体系统 v0.2 —— 整合 CL-AO v0.1 的严谨控制层与 ao-supervision-sidecar 的 mission 产品化能力。

权威设计基线：[`../ARCHITECTURE-v0.2.md`](../ARCHITECTURE-v0.2.md)。角色、通道、协议、不变量以该文档为准。

## 结构

```text
src/loopcore/
  envelope.py          Loop Bus 消息信封、开放路由表、issueFingerprint 去重
  ao_client.py         继承自 CL-AO：daemon 发现、Conversation 读写、幂等恢复
  protocol.py          继承自 CL-AO：AuditRequest/AuditReport/PlannerDecision
  observer.py          继承自 CL-AO：确定性 Observer（待合并 sidecar NO_PROGRESS 规则）
  integration_gate.py  继承自 CL-AO：确定性 Integration Gate
config/                阈值与模型配置（worker.model 等）
prompts/               角色契约提示词（Planner/Auditor/Verifier/Worker）
tests/                 离线测试（MockTransport，不访问真实网络）
docs/                  实现证据与决策记录
```

## 不变量速查

唯一 Planner；Observer/Gate 是程序不是 agent；Auditor 只读；所有消息幂等 fail-closed；每个 thread 有界、超限转 HUMAN；不使用 Codex。
