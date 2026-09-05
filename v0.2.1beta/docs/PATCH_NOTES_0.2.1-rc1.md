# CL_AO 0.2.1-rc1 修改说明

基线：组员 v0.2，commit `4d3e8e6b5e70bab868b2eef0d28c7742dea044ba`。
原发布 ZIP 已独立计算 SHA-256，与提供值一致；包内 92 条 SHA256SUMS 均通过。

## 实際修改

1. `jsonschema==4.26.0` 成为必需依赖；删除生产协议校验的弱 fallback。
2. 启动时检查 Schema，重复调用复用 validator；校验 TaskSpec、AuditResult、PlannerAction、VerifierResult 和 MissionPlan 的嵌套类型、必需字段、枚举、数值范围，并拒绝 NaN/Infinity。
3. 旧 `.venv` 缺 jsonschema 时，启动器会进入 bootstrap，防止“代码更新但环境未更新”。
4. Verifier 不再把错误格式的 `ac_checks` / `anti_gaming` 变成空数组；错误响应必须进入原有协议错误处理。
5. 新增完整校验和 Provider 边界回归测试，以及离线失败→反馈→修复复测（若测试报告列出）。
6. 预检单元测试使用pytest 自动恢复的 Python 能力夹具，避免运行机版本遮蔽 Git/AO/Codex 分支；不减少任何用例；增加 3.13/3.14 拒绝用例，并将 Python 可执行路径断言改为精确检查 PATH 解析结果与参数。实际产品仍要求 CPython 3.12.x，不放宽该限制。
7. 新增一键离线检查和 Windows CI 配置；尚未在 GitHub 执行 CI。
8. 恢复上一版已提供的 LICENSE、AO 固定版本/校验信息和第三方归属，纳入 release manifest。
9. 提供新版本标识与本机验收任务；发布名称不再重用原 v0.2 附件名。页面展示统一为 CL_AO；旧脚本文件名继续兼容。

## 刻意不改

- 不新增 Agent，不换模型，不修改 AO。
- 默认单 Worker、条件 Gate-first、Mission 终局 Verifier、自动推送关闭均保留。
- `MissionController`、任务状态机、预算和 Observer 触发阈值不重构。
- 不把代码“修补已完成”等同于“真实自动纠偏验收已通过”。
- 最终集成失败目前仍可转 HUMAN；不声称已经实现自动集成返工。
- 不处理全部遗留架构、指纹合并、配置裁剪或进程树风险。

## 验证范围

本次在 Linux/Python 3.13 审查环境运行离线测试，不包含 Windows GUI、真实 AO、真实 Codex/GLM 调用。
原始源码在该环境为 413 passed、25 failed；其中 24 个是 Python 3.12 前置条件遮蔽，1 个是路径断言仅接受 Windows 文件名。
新测试夹具只隔离预检单元测试的环境假设，不修改运行期平台要求。详细实际测试结果见工作区随附验证报告。
当前环境 pytest 与上游精确锁定版本可能不同，Windows bootstrap 后必须按锁定依赖再跑一次。

## 当前状态

`SOURCE_PATCHED / WINDOWS_LIVE_PENDING`。
上游旧 live PASS 属于原 v0.2，不能沿用为本候选的实机验收。未上传、未合并 GitHub。

## 发布条件

先由 `scripts/Test-Local.ps1` 完成 Windows 离线检查，再由本地模型执行
`LOCAL_VALIDATION_TASK.md` 的真实失败恢复任务。失败即停，不重装 AO、不放松测试、不编辑 SQLite 终态。
通过后再提交新分支并从清洁 commit 构建 Release。发布许可沿用旧 R2 文本，团队在公开前核对贡献授权。

本轮完整离线结果：Linux / Python 3.13.5 下 502 项通过（40.04 秒）；
未调用 AO/模型，Windows 3.12 实机和锁定依赖仍需本地重跑。
