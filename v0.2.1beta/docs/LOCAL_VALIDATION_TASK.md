# 给本地 GLM / Claude 的任务：只做 CL_AO 修补版本机验证

请直接执行，不要重新设计架构，也不要再次重装已经运行成功的 AO。
本任务不允许推送 GitHub、创建正式 Release、改动 main、复制或读取任何登录密钥。

## 输入与工作区

用户已解压 `CL_AO-v0.2.1-rc1-workspace.zip`，根目录为 `CL_AO-next`。
当前产品目录是 `交付/closed-loop-v2`。这是一份源码快照，没有原 Git 历史；
不要把它当成旧产品目录的覆盖补丁。旧项目、ao-data 和未完成会话全部保留。

## 一、先运行离线检查

在工作区根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '.\交付\closed-loop-v2\scripts\Test-Local.ps1'
```

该脚本仅在新产品目录准备 Python 3.12 `.venv`、安装锁定依赖、运行 pytest / compileall / 单任务 dry-run。
记录实际测试数和 `.local-checks` 日志。任何失败立即报告，不删测试、不改预期断言、不降依赖版本、不宣称通过。
已有 Python 3.12 找不到时只报告实际位置/PATH问题；不要擅自替换全局 Python。

## 二、复用已安装 AO

先只读检查实际 AO 安装位置、runfile 与 Codex 登录状态。此前已知候选路径：

```text
D:\AI-Worker\ao-app\resources\daemon\ao.exe
D:\AI-Worker\ao-data\ao.run
```

只在实际文件存在且当前 AO daemon 正常时，为当前 PowerShell 进程设置：

```powershell
$env:CLAO_AO_BIN = 'D:\AI-Worker\ao-app\resources\daemon\ao.exe'
$env:CLAO_AO_RUN_FILE = 'D:\AI-Worker\ao-data\ao.run'
```

若实际位置不同，读取当前启动说明确定，不重新安装。不要打印 runfile 内的认证字段或读取模型 credential 文件。
本项目 Controller 仍使用 Codex 作为已验证 Provider；GLM 在此任务中是执行修改/验证的助手，不是要求替换全部 Provider。

## 三、做一条真实失败恢复验收

仅使用新建的一次性测试目录和本地 bare origin，不连接正式项目远程。
建议根目录：`D:\AI-Worker\CL_AO-live-check-<时间戳>`。
先从 `ao ... --help` 核实本机实际 CLI 参数，再通过 AO 公共命令注册测试仓库。
不要猜端点，不直接改 AO 数据库，不修改已有 Project 配置。

任务要求：初始 app.py 中 divide 实现错误（如返回 a*b）；两个固定测试验证
`divide(6,3)==2` 与除零抛出 ValueError。测试文件在任务前提交并记录 SHA-256，
Worker 只允许修改 app.py；设单 Worker、固定预算、禁止自动合并主分支/推送。

为了可重复验证反馈通道，可以把第一次任务明确限定为：
“先复现当前测试失败并报告，暂时不改代码，等待控制程序的下一条修补指令。”
这是受控故障注入演示，必须标注；不得当作自然故障率或效率提升的对照实验。

正式验收必须出现真实事件与动作：
Worker 的失败 → Gate 或 Observer 发现 → 真实 Auditor → 真实 Planner → AO send →
同一 Worker 修复 → 对新代码重新 Gate → Mission Final Gate / Verifier → MISSION_DONE。
若三次重复运行会被模型拒绝，至少完成一次真实 Gate FAIL 的自动返工，不伪造日志。
如果系统只能正常成功而没有调用 Auditor/Planner，就只能记录 HAPPY_PATH_PASS，不能记录 RECOVERY_PASS。

操作要求：
- 新 Mission ID、新 runtime，不覆盖旧运行记录。
- 不直接向 Worker 人工发送修复内容，不编辑 SQLite，不删除 HUMAN 状态后续跑。
- 可以由用户完成登录或批准明确的初始化权限；这些不是业务反馈，须记录是否发生。
- 状态到 HUMAN/FAILED 或连续两次同类无进展时停止；不要无边界重跑。
- 除启动前写入受控故障外，不由脚本或 GLM 在后台替 Codex 修复 app.py。
- 测试前后哈希一致；目标 main/origin 不应被自动写回。
- 保存 Mission ID、Worker ID、Audit/PlannerAction、Gate 输出、最终代码 diff、测试哈希和可复现命令。
- 日志保存在新目录中，出包前脱敏，不上传 ao-data、cookies 或真实账号信息。

## 四、只补必要的环境接线问题

允许修当前新工作区的启动路径或与新增 jsonschema 严格校验直接相关的回归；
每项修改需给出文件、原因、diff，并重跑相关测试。不得换模型/删审计/跳过校验以制造 PASS。
新发现架构缺陷单独列出，不顺手开始大重构或自动集成返工功能开发。

## 五、给用户的结果（不要长篇复述计划）

```text
OFFLINE_TESTS = 实际通过/失败/跳过数
PYTHON / PYTEST / JSONSCHEMA = 实际版本
AO_VERSION = 实际版本
HAPPY_PATH = PASS | FAIL | NOT_RUN
REAL_FAILURE_RECOVERY = PASS | FAIL | NOT_RUN
AUDITOR_CALLS / PLANNER_CALLS / AO_SEND = 实际次数
MISSION_ID / WORKER_ID = 实际值
FINAL_STATE = 实际值
TEST_FILES_UNCHANGED = true | false
MAIN_ORIGIN_UNCHANGED = true | false
MANUAL_BUSINESS_FEEDBACK = YES | NO
EVIDENCE_DIR = 路径
BLOCKER = 最小阻塞点
GITHUB_CHANGED = NO
```

完成后停止。通过后再由用户决定是否推送分支与发布。
若要保留 Git 历史：在已知基线 commit 的真实 clone 中用附带 patch 先 `git apply --check`，
再应用并提交到新分支；不能直接覆盖 remote/main 或强制推送。
