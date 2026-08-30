# 独立 PV（产品验证）任务书 — closed-loop-v2 闭环智能体控制台

> 任务性质：独立第三方产品验证。你不需要、也不应该信任开发方的结论；
> 一切以你自己实跑收集的证据为准。发现问题不要修，记录并给出最小修复建议。
> 全程所有操作只能在 `E:\智理杯智能体大赛` 目录内进行。

## 0. 被测对象

- 面板程序：`E:\智理杯智能体大赛\closed-loop-v2\`（`panel\server.py` 零依赖后端 +
  `panel\index.html` 单页前端，端口 7100）
- 目标演示仓库：`E:\智理杯智能体大赛\closed-loop-demo`（master 当前应在 `c04b8193`，
  含 add/divide/square/decrement/negate（app.py）与 cube/double/half（math2.py））
- AO daemon：http://127.0.0.1:3001（应已在运行；`ao-data\ao.run` 存在）
- 架构一句话：确定性程序做检测（Observer/Gate），语义判断由 Agent 做
  （Planner 唯一决策者 / Auditor 只读审计 / Verifier 独立验证），Worker 按需孵化，
  全程有界循环 + HUMAN 人工兜底。用户可在面板上对任意 agent 下指令
  （发非 Planner 的指令会同步镜像给 Planner）。

## 1. 环境自检（先跑，任何一项失败直接记 BLOCKER）

| # | 检查 | 命令 / 方法 | 预期 |
|---|---|---|---|
| E1 | AO daemon 存活 | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3001/` | 有 HTTP 响应码（404 也算存活） |
| E2 | 单元测试全绿 | `cd E:\智理杯智能体大赛\closed-loop-v2`，然后 `set PATH=%CD%\.venv\Scripts;%PATH%` 后 `.\.venv\Scripts\python.exe -m pytest tests/ -q`（环境变量 `PYTHONPATH=src`） | **247 passed** |
| E3 | master/origin 同步 | `git -C E:\智理杯智能体大赛\closed-loop-demo rev-parse --short HEAD` 与 `git -C E:\智理杯智能体大赛\closed-loop-demo-origin.git rev-parse --short master` | 两者一致（c04b8193） |

## 2. 功能验证场景

启动方式：双击 `closed-loop-v2\启动面板.bat`（或 `.\.venv\Scripts\python.exe panel\server.py`），
浏览器打开 http://127.0.0.1:7100/ 。

### S1 空闲态渲染
- 预期：历史任务列表可见（MISSION-QUICK-014 为 MISSION_DONE；MISSION-PANEL-20260830-203226
  为 HUMAN）；Agent 拓扑图可见（虚线=单向、实线=双向）；时间参数 4 个旋钮有值；
  「DONE 后自动合并 master」复选框默认**不勾选**。
- 证据：整页截图。

### S2 历史任务只读查看（attach）
- 对 MISSION-PANEL-20260830-204443 点「查看」。
- 预期：双子任务卡片均 DONE；事件流含 auditor→planner→integration_gate→verifier 全链；
  「最近证据」区含 Verifier PASS 与「自动合并 master: ✔ …已推 origin」；
  memory.md / project.md 两个 tab 有非空内容。
- 红线：attach 必须是**只读**——操作前后该任务 runtime 目录的 state.db 不应有新写入
  （记录文件修改时间对比）。

### S3 一键任务全闭环（核心场景，限时 ≤ 8 分钟）
- 点「新建任务」，填入：
  - 目标：`在 app.py 中新增函数 clamp01(x) 把 x 截断到 [0,1] 区间；在 math2.py 中新增函数 sign(a) 返回 -1/0/1。各配一个 pytest 测试。`
  - 验收条件两行：`app.py 中存在 clamp01 且 clamp01(1.5)==1、clamp01(-2)==0，测试通过` /
    `math2.py 中存在 sign 且 sign(-3)==-1、sign(0)==0，测试通过`
  - 子任务上限：2
- 预期：自动经历 分解→双 Worker 并行→（Observer 捕获活动）→完成审计→Auditor PASS→
  Planner 放行→Gate pass→Verifier PASS→DONE→集成合并→终局门禁→Mission Verifier→
  **MISSION_DONE**；全程**无任何人工干预**；无 LOOP_ERROR 告警。
- 证据：事件流截图 + 终态截图 + `runtime\<任务ID>\state.db` 中 missions 行的 state 字段。

### S4 自动合并开关（承接 S3）
- 前置：S3 发起**之前**勾选「DONE 后自动合并 master」并点「应用」。
- 预期：S3 到达 MISSION_DONE 后无需任何操作，本地 master 与 origin 同时前进到
  新的集成头；面板「最近证据」区出现绿色「自动合并 master: ✔」行。
- 验证命令：同 E3 的两个 rev-parse，结果应一致且不再是 c04b8193。
- 反向验证（可选）：不勾选开关再跑一个任务，master 应**不动**。

### S5 用户指令通道（实跑验证）
- 在一个任务**运行中**（Worker 工作期间），底部指令栏：目标选某个 Worker，发送
  `注意保持函数实现尽量简单`。
- 预期：toast 显示「已发送，并同步 Planner」；该任务 `runtime\<ID>\bus_traffic.jsonl`
  出现一条 sender=user 的 USER_DIRECTIVE 记录。
- 再对 Planner 发送一条指令，预期 toast 为「已发送（仅 Planner 可见）」。

### S6 时间参数实时生效
- 运行中把「Observer 轮询」改为 3 并点「应用」。
- 预期：toast 成功；`GET /api/state` 返回的 config.poll_seconds == 3；
  不需要重启任务。

### S7 HUMAN 兜底证据（历史复现，不需重跑）
- 对 MISSION-PANEL-20260830-203226 点「查看」。
- 预期：状态 HUMAN，reason 含 `merge conflict`；事件流中 S1 曾 DONE、S2 停在
  WORKER_RUNNING。该任务证明：集成冲突时系统正确地交人工，而不是强行合并。

### S8 断点续跑
- 对任意非终态历史任务（如显示「?」的 MISSION-QUICK-013）点「续跑」。
- 预期：要么正常进入运行（状态推进），要么面板给出明确错误提示；
  **不允许**出现静默失败或无提示卡死。记录实际行为。

### S9 停止按钮
- 任务运行中点「停止」。
- 预期：任务停下，`running` 变 false；该任务之后可以「续跑」恢复。

## 3. 约束（违反即验证无效）

1. 不得修改 `closed-loop-v2\src\` 下任何源码（你是验证方，不是修复方）。
2. 不得使用 codex 模型；不得手动编辑 closed-loop-demo 仓库代码帮 worker 作弊。
3. 单个任务预算上限按面板默认；总 LLM 调用异常增多（同一任务反复重试）要记录。
4. 测试结束后：停止面板服务器（7100 端口不得残留进程），恢复旋钮默认值
  （5 / 300 / 600 / 300），不勾选自动合并。

## 4. 交付物

写回 `E:\智理杯智能体大赛\PV-验证报告.md`：
- 每个场景 PASS / FAIL / BLOCKER + 证据（截图路径、命令输出、state.db 字段）；
- 每个 FAIL 给出最小修复建议（文件 + 改法），不要直接改；
- 末尾给结论：**可发布 v0.2 / 需修复后再验**。

## 5. 开发方已知项（不算新发现，但可复核）

- 老存档 MISSION-QUICK-012/013 状态显示 "?"（历史数据无状态行，属诚实显示，非 bug）。
- 自动合并开关为内存态：重启面板后恢复默认关闭。
- 窄窗口下中列拓扑图偏小（响应式已处理，极端窄时仍需横向滚动）。
