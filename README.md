# Agent Runtime Manager (ARM)

面向本地 AI Agent（Claude Code / Codex）的**运行时管理层**——合盖+电池场景下，Agent 任务像开盖插电一样全速运行；任务结束自动释放一切。生命周期驱动，不是无脑防睡眠工具。

## 当前形态（单进程）

`arm app` = 引擎线程（2 秒/拍：感知 → 决策 → 施加保护）+ 托盘 + 原生窗口 + 本地 UI（127.0.0.1:8620，随机 token，仅本机）。

**二态模型**：ARM 在托盘里 = 全职保镖（自动检测任务并保护；电源键临时"不动作"防误按）；退出 = 一切休息（电源、按键、看门狗全部还原）。

**自愈闭环**：进程内互护 + OS 级看门狗计划任务（全死 ≤5 分钟复活）+ hooks 哨兵 + 电源快照崩溃自愈 + transcript 旁路感知（hooks 丢了也能保护）。详见 `docs/Self_Healing_Manual.md`。

## 安装与开发

```bash
uv sync                                   # 创建环境
uv run --with pytest python -m pytest -q  # 全量测试
uv tool install --force .                 # 装全局（务必先停所有 arm 进程）
arm doctor                                # 环境自检
```

## 常用命令

```
arm app            桌面应用（日常形态：托盘 + 窗口 + 引擎一体）
arm status         保护状态与 Agent 会话
arm watchdog       看门狗单次检查（供计划任务调用）
arm doctor         环境自检
arm init --yes     注入 Claude hooks（--undo 卸载并抑制重注入）
arm protect        手动开启保护
arm release        手动释放保护
arm watch          终端实时监视
arm ui             浏览器控制台（本机）
```

## 文档

- `docs/Self_Healing_Manual.md` —— 自愈机制全景与排障决策树（**出问题先看这个**）
- `docs/System_Change_Ledger.md` —— 系统级变更台账（计划任务/hooks/注册表，agent 修改前必须登记）
- `CLAUDE.md` —— Claude 会话项目指南
- 其余（PRD / Architecture / Task_Breakdown / Polish_Blueprint / Handoff）为历史文档，描述合并前架构，仅作参考
