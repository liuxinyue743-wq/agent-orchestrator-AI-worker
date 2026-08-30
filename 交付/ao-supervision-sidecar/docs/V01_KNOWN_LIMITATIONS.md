# V0.1 已知限制

1. **真实 Auditor/Planner 实跑未完成**：`ClaudeCliAuditorProvider` 与
   `AOOrchestratorPlannerProvider` 的代码与参数已就绪、schema 验证已就绪，
   但一次性真实联调被 harness 安全分类器持续不可用阻断（非接口/设计缺陷）。
   dry-run 全链路（真实 AO 事件 → REPEATED_ERROR → Audit LOCAL_FIX →
   Planner SEND_LOCAL_FIX）已验证通过。

2. **AO 动态端口**：daemon 每次启动端口不同，adapter 现从 `ao.run` 读取；
   若 `AO_RUN_FILE`/`AO_DATA_DIR` 未设则回退 3001（可能失败）。

3. **activity 无独立时间戳**：事件时间取 turn requestedAt → worker
   lastActivityAt → now，粒度为 turn 级（审计 §3）。

4. **SSE 无项目过滤/无心跳**：客户端按 sessionId 过滤；空闲自动重连 +
   Last-Event-ID 续传。

5. **Auditor 一次性预算**：`--max-budget-usd` 默认 0.20；复杂证据可能不足，
   可在 `ClaudeCliAuditorProvider(budget_usd=...)` 调高。

6. **GLM provider**：`.codex/config.toml` 的 GLM 块因 Codex 不再支持
   `wire_api="chat"` 已改为 `"responses"`；GLM 端点对 Responses API 的兼容性
   未验证（V0.1 不使用 GLM，仅 Codex/Claude Code）。

7. **单 Worker**：V0.1 严格单 Worker；MAX_PARALLEL_WORKERS 隐含为 1。

8. **无自动合并**：Gate 通过仅置 DONE，不自动 merge 到 master（人在回路）。
