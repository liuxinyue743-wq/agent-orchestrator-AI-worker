# AO 版本升级验收清单

> 用途：每当 Agent Orchestrator（AO）官方发布新版本，用本清单在 **不动现有环境** 的前提下完成兼容性验收；通过后才切换。
> 首次实例：2026-08-30，v0.12.7 → v0.12.9，结论"纯增量、可升级"，已按本流程完成实际升级。
> 配套工具：[`ao-openapi-diff.py`](ao-openapi-diff.py)（两版 OpenAPI 契约自动对比，见第 4 步）。

## 核心原则

1. **永不直接升级正在使用的环境**。先在数据副本上验证，通过后才切换。
2. **契约对比是唯一硬性标准**：路由/操作零删除、零改名，CL-AO 关键字段全部存在。新增（additive）是安全的。
3. **数据目录与版本解耦**：AO 应用通过环境变量 `AO_DATA_DIR` 选择数据目录（未设置时回退 `%USERPROFILE%\.ao`）。升级只换程序目录，不动数据目录。
4. **回滚随时可用**：旧版本目录原地保留，换回即回滚。

## 第 0 步：保存当前基线（升级前）

```powershell
# 确认当前 daemon 在线并记录版本
curl http://127.0.0.1:3001/healthz

# 保存当前契约作为基线（在当前文件夹执行）
curl -s http://127.0.0.1:3001/api/v1/openapi.yaml -o baseline-openapi-<旧版本>.yaml
```

同时记录：项目数、会话数、各会话 activity 状态（升级后用于比对）。

## 第 1 步：下载并校验安装包

从 `https://github.com/Untrivial-ai/agent-orchestrator/releases/tag/v<新版本>` 下载 `agent-orchestrator-win32-x64.exe` 到当前文件夹。

**必须校验 SHA-512**：下载同 release 的 `latest.yml`，对比其中 `sha512` 字段：

```powershell
# Python 一行校验
python -c "import hashlib,base64; print(base64.b64encode(hashlib.sha512(open('ao-<新版本>-setup.exe','rb').read()).digest()).decode())"
```

校验值与 `latest.yml` 不一致则**终止升级**，重新下载。

## 第 2 步：解压到独立测试目录

NSIS 安装包可用 7-Zip 直接解压（不解包安装器、不写注册表）：

```powershell
mkdir ao-app-<新版本>-test
.\tools\7zr.exe x ao-<新版本>-setup.exe -oao-app-<新版本>-test -y
```

确认存在 `ao-app-<新版本>-test\agent-orchestrator.exe` 和 `resources\daemon\ao.exe`。

## 第 3 步：在数据副本上启动新 daemon

```powershell
# 3.1 复制数据目录（ao.db 必须用 SQLite 备份 API 一致性复制，不能直接拷文件）
#     见下方 Python 片段；其余目录直接复制。
python - <<'EOF'
import sqlite3
src = sqlite3.connect('file:ao-data/data/ao.db?mode=ro', uri=True)
dst = sqlite3.connect('ao-data-upgrade-test/data/ao.db')
src.backup(dst)
EOF

# 3.2 用新二进制在副本上启动（独立端口，与现行 3001 隔离）
#     环境变量：AO_DATA_DIR 指向副本，AO_PORT 指向空闲端口
#     启动命令：ao-app-<新版本>-test\resources\daemon\ao.exe daemon
```

**判读启动日志**：
- `daemon listening` + `/readyz` 返回 `"status":"ready"` → 数据库迁移成功；
- `reconcile ... outside managed root` 警告是**副本路径差异的正常产物**，不是升级问题（真实升级时数据目录路径不变，不会出现）；
- 出现数据库报错、panic、端口起不来 → **终止升级**。

## 第 4 步：兼容性验证（全部通过才算可升级）

| # | 检查项 | 命令 / 方法 | 通过标准 |
|---|---|---|---|
| 4.1 | 项目与会话完整 | `GET /api/v1/projects`、`GET /api/v1/sessions` | 数量与基线一致，字段齐全 |
| 4.2 | 会话快照 | `GET /api/v1/sessions/{id}/conversation?limit=5` | 含 `turns`/`messages`/`latestSequence`；message 含 `role`/`origin`/`text`/`turnId`/`revision` |
| 4.3 | 工作区接口 | `GET /api/v1/sessions/{id}/workspace/files` | 干净仓库仍用 `status:"unmodified"` 表示；`truncated=false` |
| 4.4 | SSE 事件 | `GET /api/v1/events?after=0` | 能回放历史事件 |
| 4.5 | **OpenAPI 对比** | `python ao-openapi-diff.py baseline-openapi-<旧版本>.yaml test-openapi-<新版本>.yaml` | **退出码 0**；输出 `COMPATIBLE` |
| 4.6 | 人工 skim 全量 diff | `diff baseline-*.yaml test-*.yaml` | 只有新增（`>` 行），无删除/改名/必填化 |

退出码非 0 或 4.6 发现删除项 → **终止升级**，把 diff 结果带回给 CL-AO 适配（适配点只应在 `ao_client.py`）。

## 第 5 步：执行升级（仅在第 4 步全部通过后）

```powershell
# 5.1 优雅停止：先 daemon 后应用
curl -X POST http://127.0.0.1:3001/shutdown
# 等待 5 秒后确认 3001 不再响应；残留 agent-orchestrator.exe 进程再 taskkill

# 5.2 切换目录（旧版原地保留为备份）
mv ao-app ao-app-<旧版本>-backup
mv ao-app-<新版本>-test ao-app

# 5.3 以相同数据目录启动新应用
#     环境变量 AO_DATA_DIR=E:\智理杯智能体大赛\ao-data 必须与旧环境一致
```

**注意**：AO 重启会使所有活跃会话按设计转为 terminated（任何版本重启都如此），升级前确保没有正在执行的关键任务。

## 第 6 步：升级后确认

- `readyz` 返回 `ready` 且 `workingDirectory` 指向正确数据目录；
- 项目数 / 会话数 / 会话历史与基线一致；
- 从 app.asar 内 `package.json` 确认新版本号（`app-state.json` 的版本字段由 reconcile 延迟更新，不能作为即时判据）；
- 清理测试副本目录；保留基线 yaml、安装包和旧版本备份。

## 回滚

停掉应用 → `mv ao-app ao-app-<新版本>-broken` → `mv ao-app-<旧版本>-backup ao-app` → 以相同 `AO_DATA_DIR` 启动。数据目录全程未被修改，无需回滚数据。

## 已知边界

- 本清单验证 **REST 契约与数据兼容**，不验证 Electron UI 的交互细节（UI 自包含于新版本内，风险低）。
- AO 无数字签名之外的官方兼容性承诺；`latest.yml` 的 SHA-512 只证明下载完整，不证明接口不变——接口判断以第 4 步为准。
- 若未来版本出现破坏性变更，CL-AO 应新增版本探测并在未适配版本上 fail-closed 拒绝运行。
