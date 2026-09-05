# CL_AO v0.2.0-beta 发布状态

## 发布结论

本版本可以作为 **Windows 本地技术预览版 / 比赛演示版** 发布，不应标记为生产稳定版。

## 已核验

- 清理后的源码不包含 AO 数据库、Electron Profile、虚拟环境、缓存、历史 worktree 或认证材料；
- 离线测试：334 项通过；
- Web Panel、MissionController、Observer、Auditor、Planner、Gate、Verifier 和 StateStore 均有源码与测试；
- AO 版本锁定为 v0.12.9，提供官方安装器自动下载与 SHA-256 校验；
- 源码中的开发机 E 盘路径和个人用户 Git Bash 路径已移除；
- 默认模型配置改为使用本机 Claude CLI 的默认模型，其他提供方由用户显式配置。

## 尚未独立认证

- 在第二台全新 Windows 电脑上的零人工端到端安装；
- 任意代码仓库、任意模型和任意网络环境下的兼容性；
- 生产级安全、可用性、性能和长期无人值守运行；
- 所有最终集成失败都能够自动修复；部分高风险或预算超限场景会进入 `HUMAN`。

## 合理对外表述

> CL_AO v0.2.0-beta 是面向预注册 Git 项目的 Windows 本地闭环多智能体开发原型。它可用于比赛演示和受控开发实验；用户需安装 AO、模型 CLI 并完成本机认证。
