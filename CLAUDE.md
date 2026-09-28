# ARM（Agent Runtime Manager）项目指南

写给在本项目工作的 Claude 会话。历史文档看 `docs/`（均带"历史文档"标注）；**现状永远以本文档 + `docs/Self_Healing_Manual.md` 为准**。

## 一句话使命

本地 AI Agent（Claude Code / Codex）运行时管理层：**合盖+电池，任务全速跑；任务停，自动释放**。生命周期驱动，不是防睡眠工具。

## 架构心智模型（单进程）

`arm app` = 引擎线程（2 秒/拍：感知 → 决策 → 施加保护）+ pywebview 窗口 + pystray 托盘 + 本地 UI（127.0.0.1:8620，随机 token 仅本机）。

**二态模型（2026-09-28 定稿，用户拍板）**：app 在托盘 = 全职保镖（自动检测任务并保护；电源键运行期钉"不动作"防误按）；退出 = 一切休息。**没有中间态**，不要给保护/布防加记忆类语义。

## 三信号感知（任一活跃即保护，宁可误保护）

1. hooks（精准）：SessionStart/UserPromptSubmit/Stop/SessionEnd → `arm hook-ingress` 落库。**桌面端会话不发提示词级 hooks（实测），transcript 是正式兜底信号**
2. transcript 旁路（可靠）：mtime + 尾部判定，静默 <90s = BUSY（已做三级缓存，改感知代码必须保持缓存契约）
3. 进程探测（兜底）：按包名/路径正则精准匹配，勿按 exe 名（ChatGPT.exe 属 MSIX 包）

## 常用操作

```bash
uv run --with pytest python -m pytest -q   # 全量测试（当前 119，改动后必须全过）
uv tool install --force .                  # 装全局——务必先停所有 arm 进程（否则 arm.exe 装残）
arm doctor && arm status                   # 自检 + 状态
tail ~/.arm/arm.log                        # 排障第一入口
```

引擎测试已隔离（电源/进程探测/transcript 全 mock、data_dir 重定向）；新增引擎测试必须沿用 `_engine()` 助手，否则 pytest 会真钉真机电源、真拉起 app。

## 系统级变更纪律

任何计划任务/hooks 注入/注册表/全局安装改动，**必须先登记 `docs/System_Change_Ledger.md`（台账），完工后更新状态**。用户说"按台账对账"= 逐项核对系统实况、汇报差异、经确认才删。

## 出问题按序查

1. `docs/Self_Healing_Manual.md`（自愈机制全景表 + 排障决策树 + 已知边界）
2. `~/.arm/arm.log`（引擎日志，轮转 5MB×3）
3. 台账对账：`docs/System_Change_Ledger.md`

## 红线（血泪教训速查，完整版在手册与记忆）

- 单实例锁是命名互斥体（`WinDLL(use_last_error=True)` + `ctypes.get_last_error()`）——改回 `windll.GetLastError()` 会重现双引擎风暴（2026-09-28 事故根因）
- 引擎线程必须非 daemon（否则退出时 finally 不跑 → 电源滞留）；release() 必须还原电源快照
- 电源读写必须走 `_power_lock()`；电源键钉组与保护钉组生命周期不同（app 期 vs 保护期），快照文件分离
- CLI 输出禁止 ✓✗⚠（GBK 控制台崩，用 [OK]/[FAIL]/[!]）；pythonw 下 `sys.stdout=None`，echo 要守卫
- SQLite WAL 写后必须显式 `conn.commit()`
- 浏览器 JS 永远 data-act/data-sid 属性 + 事件委托，禁拼内联 onclick
- 杀进程按参数列表精确匹配 + 排除自身 pid；powercfg/schtasks 的 `/x` 参数别走 Git Bash 直调（用 PowerShell 或 subprocess）
- 写 VBS/注册脚本：raw string + ASCII + CRLF
- Git Bash 会吃 PowerShell 的 `$_` 和 `/F` 参数

## Git 规范

- 分支：单人项目，`main` 直接提交即可；大改动开 `feat/xxx` 短命分支
- 提交信息：Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`，主题用中文说清"为什么"）
- 提交前跑全量测试；`uv.lock` 入库；`.claude/settings.local.json` 不入库
