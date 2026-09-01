# CHANGELOG

## 2026-09-01 — 复核修复轮（对照外部复审 #1/#2/#6/#7/#8）

### 修复

- **#1/#2 命令审批双轨分歧（本轮唯一实质安全缺口）**：上一轮的
  `git restore/checkout` 收紧只落在纯函数 `approvals.is_safe_command`，而生产
  路径走的是 `ClosedLoop._is_gate_command` → `_is_safe_segment`——那里仍放行
  restore/checkout，且 `rest.startswith("-m pytest")` 前缀判断可被
  `python -m pytest_evil` 绕过。现在 `_is_safe_segment` 的叶政策（gate 命令
  匹配 / `python -m pytest` 精确 token / git 白名单）统一委托给单测覆盖的
  `is_safe_command`，生产包装只保留需要 worktree 上下文的额外项（子shell 解包、
  `cd <worktree>` 前缀、只读探测命令 ls/cat/which 等）。两条路径从此同源。
  新增 `tests/test_gate_command_policy.py`（28 项，直接打生产路径）。
- **#6 pg `SET search_path` 插值**：PG 标识符无法走绑定参数，正确修法是严格
  白名单。新增 `state_store_pg.validate_schema()`（`^[a-z0-9_]+$`，在任何建连
  之前抛出），`PgStateStore.__init__`、`connect_readonly`、面板
  `list_missions` 三处插值点全部接入。
- **#7 CLI `--db` 路径穿越**：`--db ../../x.db` 原先可逃逸 `runtime/`。新增
  `storage.safe_runtime_name()`（单路径段、禁 `..`），`mission_cli` 与
  `closed_loop_cli` 均已接入。

### 评估后保持现状

- **#8 `_shellish` 启发式**：它守的是 Planner→Worker 的消息文本（不是内核直接
  执行的命令；真实命令还有 worker 侧审批层把守）。加入 `;`/`>` 会对代码风格
  的修复指令误伤并直送 HUMAN——正是簇六修过的 'model' 误报同款失败模式。
  维持现状，记录为已评估的残余风险。

### 新增（2026-09-01 当晚补）

- **面板可选目标仓库**：新建 Mission 表单新增「目标仓库」下拉，选项实时读自
  AO 项目登记表（`ao.db` projects 表，排除 scratch/已归档）；后端
  `GET /api/projects` + 启动前成员校验，未登记或带路径分隔符的 project_id
  一律 400 拒绝并列出可选项。此前表单无此字段，所有任务只能落进默认的
  closed-loop-demo。

### 测试

- 全套 **334 项通过、0 失败**（上轮 302）。

## 2026-08-31 — 商业化加固轮（联合审计修复）

覆盖来源：联合审计报告（架构与交付审计 docx）、ChatGPT 复核清单、面板实测暴露的亮灯问题、以及"数据库不可绑定单一实现"的产品要求。

### 修复

- **Verifier 旧结论复用（P0，实测复现）**：同一 Worker 返工后，Verifier 会把上一轮
  的 FAIL 结论当成"崩溃恢复"直接复用，导致新证据永远不被验收。现在 verdict 与证据
  版本绑定（`verify_id = stable_id(task, worker_session, gate_round)`，新增
  `StateStore.gate_round()`）：返工后 gate 重跑 → 证据版本变化 → 必然重新调用
  Verifier；只有证据未变的真崩溃恢复才复用旧结论（不重复调 LLM）。
  新增 `tests/test_verifier_rework.py`（3 项）。
- **自动审批收紧**：`git restore` / `git checkout` 会丢失工作区改动，移出
  `is_safe_command` 白名单，与 `git reset --hard` 一样一律转人工审批。
  `tests/test_approvals.py` 新增回归断言。
- **Verifier→Worker 直连通道删除（审计建议）**：路由表移除
  `("verifier", "FIX_REQUEST", "worker:")`，Worker↔Verifier 改为单向
  （Worker 可发 VERIFY_REQUEST，Verifier 只回 Planner）。枚举值保留以便解析
  旧存档/日志；`tests/test_envelope.py` 相应断言改为"拒绝"。
- **面板拓扑图两处失真**：① 删除 Verifier→Worker 动态假连线（内核无此数据流）；
  ② 亮灯从"最近 transition actor"改为按子任务状态机位置驱动——修复 Planner
  拆解期全程不亮、Verifier 在任务结束后常亮两处实测问题。终态全灭。
- **平台/环境相关测试**：`test_to_argv_windows_quote_stripping` 改为按解释器
  basename 断言（跨平台）；`sidecar_port/test_phase3.py::test_gate_pass` 运行前把
  当前解释器目录前置到 PATH（gate 走 shutil.which，避免误拾无 pytest 的
  托管/基础 Python）。

### 新增

- **存储后端可选**（不再绑定单一 SQLite）：
  - `loopcore.storage`：配置解析（内置默认 ← `config/default.yaml` `storage:` ←
    `config/storage.json`）、原子落盘、DSN 掩码、`make_store` 工厂。
  - `loopcore.state_store_pg.PgStateStore`：PostgreSQL 后端，每任务一个 schema
    （`clm_<mission-id>`），psycopg v3 懒加载，方法面与 SQLite StateStore 全对齐。
  - 面板新增「存储后端」卡片（下拉选后端、DSN 输入、掩码回显）；历史任务列表
    双后端并查去重；resume/attach 按存档实际所在后端自动打开（pg 存档缺 DSN
    时给出可操作报错）。
  - 新增 `tests/test_storage.py`（17 项，fake psycopg 断言 DDL/upsert/占位符）。

### 测试

- 全套 **302 项通过、0 失败**（上轮基线 280 通过 + 1 环境失败）。
- 面板冒烟：`/api/state` 返回 `config.storage`；非法 postgres 配置（缺 DSN）
  被 400 拒绝并附中文原因；attach 挂载 `MISSION-PANEL-20260831-205655`
  正常读出 DONE 状态、3 条 gate 记录、2 条验收记录。
