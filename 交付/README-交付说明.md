# 闭环多智能体系统 — 组员交付包

本文件夹是项目交付件,用于组内讨论与复现。只包含可运行源码、仓库与项目文档;
运行历史日志、缓存、虚拟环境、安装包等已剔除。

## 1. 这是什么

一个有界循环、人工兜底的多智能体控制系统:
确定性程序做检测(Observer / Integration Gate),语义判断由 Agent 做
(Planner 唯一决策者 / Auditor 只读审计 / Verifier 独立验证),Worker 按需孵化。
用户可在 Web 面板(端口 7100)对任意 agent 下指令。

权威设计基线:**`ARCHITECTURE-v0.2.md`**(角色、通道、协议、不变量以此为准)。

## 2. 目录结构

```
ARCHITECTURE-v0.2.md          权威设计基线(讨论第一份要看)
PV-独立验证任务.md            独立产品验证任务书(验收口径)
AO_UPGRADE_CHECKLIST.md        AO 版本升级验收流程
ao-openapi-diff.py            两版 OpenAPI 契约自动对比工具

closed-loop-v2/               ★ 主项目本体(v0.2 整合版,最终交付与运行对象)
  src/loopcore/               核心代码:bus / envelope / mission / observer /
                              auditor / verifier / planner / ao_adapter ...
  panel/                      控制台后端 server.py + 单页前端 index.html (7100)
  prompts/                    角色契约提示词(Planner/Auditor/Verifier)
  config/default.yaml         阈值与模型配置
  schemas/                    事件/告警/消息 JSON Schema
  tasks/                      mission 任务定义(mission-quick*.json)
  run_mission.py              一命令跑完整 mission 的 CLI
  启动面板.bat                 双击启动 Web 面板
  README.md                   项目自述
  tests/                      单元测试(PV 任务书称 247 passed)

clao-src/clao-v0.1.0/         上游控制层(CLAO,组员编写;v2 继承其
                              observer/ao_client/protocol/integration_gate)

ao-supervision-sidecar/       上游产品化能力(事件归一化/告警规则;
                              v2 继承其 event_normalizer/fingerprints)

closed-loop-demo/             演示目标仓库(app.py/math2.py;闭环对其执行
                              pytest 并合并 master)
closed-loop-demo-origin.git/  demo 的裸 origin(闭环 git push/pull 用)
```

## 3. 运行前提(组员需自备)

1. **Python 3.12+**,自建虚拟环境:
   ```bash
   cd closed-loop-v2
   python -m venv .venv
   .venv/Scripts/activate            # Windows
   pip install httpx pyyaml pytest
   ```
   仅这两个第三方依赖,其余为标准库。

2. **Agent Orchestrator (AO) daemon** 运行于 `http://127.0.0.1:3001`。
   AO 桌面端不随包交付(体积 ~450M),请自行安装:
   见 `AO_UPGRADE_CHECKLIST.md` 第 1 步,从 GitHub release 下载安装包。

3. **claude CLI**(headless)在 PATH 中 —— Planner/Auditor/Verifier 通过它调用 LLM。

## 4. 快速跑起来

```bash
cd closed-loop-v2
# 1. 单元测试(PV 任务书称应 247 passed)
set PYTHONPATH=src && python -m pytest tests/ -q

# 2. Web 面板
.\.venv\Scripts\python.exe panel\server.py
#   或双击 启动面板.bat,然后浏览器开 http://127.0.0.1:7100/

# 3. CLI 跑一个 mission(dry-run 不碰 AO)
set PYTHONPATH=src && python run_mission.py tasks/mission-quick.json --dry-run
#   真实跑(需 AO daemon 在线)
set PYTHONPATH=src && python run_mission.py tasks/mission-quick.json
```

## 5. 已剔除的内容(非项目源码)

- `ao-app/`、`ao-app-0.12.7-backup/` — AO 桌面端本体与旧版备份(各自安装)
- `ao-data/` — 本机 AO 运行时数据(各成员自有)
- `ao-smoke-test*/`、`tools/` — 早期联调 smoke 测试仓、7z 工具
- `pv_start.py` / `pv_spawn_probe.py` / `pv-s3-mission.json` — PV 验证探针脚本
- 各处 `.venv/`、`__pycache__/`、`.pytest_cache/`、`*.pyc`
- `closed-loop-v2/runtime/` — 任务运行历史(per-mission SQLite + 日志)
- `closed-loop-v2/tasks/mission-panel-*.json` — 面板自动生成的任务存档
- `ao-supervision-sidecar/runtime/` — sidecar 运行历史
- `ao-supervision-sidecar/tools_*.py` — 审计探针(sidecar README 标注可删)
- AO 安装包、OpenAPI 契约快照、启动日志、NVIDIA 日志

## 6. 关于测试目录(请确认)

按"排除测试垃圾"的口径,以下单元测试**当前仍保留**,因为它们是项目的一部分、
且是 PV 任务书"247 passed"的验证依据:
- `closed-loop-v2/tests/`(主项目)
- `ao-supervision-sidecar/tests/`
- `clao-src/clao-v0.1.0/tests/`

其中 `closed-loop-demo/tests/` **必须保留** —— 它是演示仓库自带的 pytest,
闭环运行时正是对它执行测试,删掉演示就跑不起来。

如需把上面三个单元测试目录一并剥离(只留可运行代码),在包根目录执行:
```bash
rm -rf closed-loop-v2/tests ao-supervision-sidecar/tests clao-src/clao-v0.1.0/tests
```
```
