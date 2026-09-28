# Agent Runtime Manager (ARM)

**[中文说明在下方](#中文说明)**

Keep your AI agents running at full speed — even with the laptop lid closed on battery.

ARM is a runtime manager for local AI agents (Claude Code / Codex). Instead of blindly preventing sleep, it watches the **agent lifecycle** and manages Windows power accordingly: tasks running → keep the system fully awake (lid closed, on battery); tasks done → release everything back to normal.

## Highlights

- **Lifecycle-driven**: protection follows your agents automatically — nothing to toggle before you walk away
- **Two-state model**: the app IS the switch. Running in tray = full-time bodyguard (power button pinned against accidental sleep). Quit = everything restored
- **Three-signal sensing** (prefer over-protecting over missing): hooks events + transcript watching + process detection — works even when hooks get wiped or never loaded
- **Four agents supported**: Claude Code CLI · Claude Desktop · Codex CLI · Codex Desktop
- **Self-healing closed loop**: in-process mutual watchdog → OS-level scheduled-task watchdog (revived ≤5 min after a hard death) → hooks sentinel (re-injected ≤60 s after third-party tools wipe them) → power-snapshot crash recovery → per-tick fault tolerance
- **Local web console**: token-protected, localhost-only, zero cloud

## Requirements

- Windows 10/11 (Modern Standby / S0 is the primary target — that's where lid-close freezes processes)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Quick Start

```bash
git clone https://github.com/ShawnYoung-21/Agent-Runtime-Manager.git
cd Agent-Runtime-Manager
uv sync                                   # create env
uv run --with pytest python -m pytest -q  # full test suite
uv tool install --force .                 # install globally (stop all arm processes first)
arm app                                   # launch: tray + window + engine
```

Daily use: **close the lid and walk away.** That's it. Quit the app when you want the laptop to behave normally again.

## Documentation

- `docs/Self_Healing_Manual.md` — self-healing mechanisms & troubleshooting tree (start here when something looks wrong)
- `docs/System_Change_Ledger.md` — ledger of every system-level change (scheduled tasks / hooks / registry)
- `CLAUDE.md` — project guide for Claude sessions

## License

[MIT](LICENSE)

---

# 中文说明

面向本地 AI Agent（Claude Code / Codex）的**运行时管理层**——合盖+电池场景下，Agent 任务像开盖插电一样全速运行；任务结束自动释放一切。生命周期驱动，不是无脑防睡眠工具。

## 当前形态（单进程）

`arm app` = 引擎线程（2 秒/拍：感知 → 决策 → 施加保护）+ 托盘 + 原生窗口 + 本地 UI（127.0.0.1:8620，随机 token，仅本机）。

**二态模型**：ARM 在托盘里 = 全职保镖（自动检测任务并保护；电源键临时"不动作"防误按）；退出 = 一切休息（电源、按键、睡眠全部还原）。

**自愈闭环**：进程内互护 + OS 级看门狗计划任务（全死 ≤5 分钟复活）+ hooks 哨兵（≤60 秒修复）+ 电源快照崩溃自愈 + transcript 旁路感知（hooks 丢了也能保护）。

## 安装与开发

```bash
uv sync                                   # 创建环境
uv run --with pytest python -m pytest -q  # 全量测试
uv tool install --force .                 # 装全局（务必先停所有 arm 进程）
arm app                                   # 桌面应用（日常形态）
```

## 常用命令

```
arm app            桌面应用（托盘 + 窗口 + 引擎一体）
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
- `docs/System_Change_Ledger.md` —— 系统级变更台账（agent 修改前必须登记）
- `CLAUDE.md` —— Claude 会话项目指南
- 其余（PRD / Architecture / Task_Breakdown / Polish_Blueprint / Handoff）为历史文档，描述合并前架构，仅作参考
