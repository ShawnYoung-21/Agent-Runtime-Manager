> [历史交接] 本文档是 2026-09-27 CLI 会话写给桌面端的交接，**已被根目录 `CLAUDE.md` 取代**；其中架构与基线描述已过时（现为单进程+二态电源模型），仅作历史参考。

# ARM 开发交接文档（CLI → Claude 桌面端）

> 写给接手的 Claude 桌面端会话：请先完整读完本文档，再读 `docs/Self_Healing_Manual.md`（自愈机制手册，含排障决策树），然后即可直接干活。项目目录：`C:\Users\yangchaoxin\Desktop\Agent Runtime Manager`。

## 一、项目是什么

**Agent Runtime Manager（ARM）**：面向本地 AI Agent（Claude Code / Codex）的运行管理层。核心使命一句话：

> **Windows 11 笔记本合盖 + 电池状态下，Agent 任务要像开盖插电一样全速运行、零影响**——不是防睡眠工具，而是让系统根据 Agent 生命周期智能管理运行环境。

已达成：PRD MVP 全验收标准（AC1–AC6）。产品当前处于"用户日常使用攒反馈"阶段，**等用户反馈驱动迭代，不要主动开新大项**。

## 二、当前架构（务必先建立这个心智模型）

```
arm app（单进程，日常形态）
├─ RuntimeEngine 后台线程        ← 引擎循环：感知→状态机→施加保护
├─ pywebview 原生窗口（WebView2） ← 渲染内嵌 UI
├─ pystray 托盘                  ← 状态色图标 + 右键菜单
└─ 内嵌 HTTP 服务 127.0.0.1:8620 ← 仅本机 + 随机 token（浏览器直开 403）
```

- **单进程架构**（已合并 daemon+app 双链路，用户定调"能复用就复用，不为对称拆链路"）。`arm daemon` 命令仍在但非日常入口；引擎单实例锁 "engine"，app 内嵌引擎拿锁，手动 daemon 自动让位。
- 开机自启：计划任务 **ARM-App**（AtLogOn → venv pythonw.exe `-m arm.cli app`）。ARM-Daemon 已删。
- 桌面入口：`C:\Users\yangchaoxin\Desktop\ARM 控制台.vbs`（wscript 静默拉 pythonw）。

## 三、关键目录/文件

| 路径 | 说明 |
|---|---|
| `src/arm/engine/runtime.py` | 引擎 tick 闭环（看门狗/hooks哨兵/对账/状态机/施加） |
| `src/arm/engine/state_machine.py` | Protection + AgentTracker |
| `src/arm/sensors/transcript.py` | Claude transcript 旁路（mtime+忙闲判定） |
| `src/arm/sensors/codex_transcript.py` | Codex 双源（sqlite catalog + rollout jsonl） |
| `src/arm/sensors/` 其余 | power / standby / 进程探测 |
| `src/arm/control/power_policy.py` | T15 电源组合拳（powrprof API 钉四项电源值） |
| `src/arm/control/execution_state.py` | SetThreadExecutionState 保活 |
| `src/arm/core/claude_hooks.py` | hooks 注入/健康检查/自动修复 |
| `src/arm/core/store.py` | SQLite 四表（WAL）；`get_effective_protection` 心跳过期自愈 |
| `src/arm/ui/server.py` | 内嵌 UI（单文件内嵌 HTML，token 锁死） |
| `src/arm/ui/app.py` | NativeApp（托盘+窗口+引擎内嵌+互护） |
| `src/arm/cli.py` | typer CLI（app/status/watch/ui/doctor/init/protect/release） |
| `docs/Self_Healing_Manual.md` | **自愈机制手册——出问题先拉它** |
| `~/.arm/` | 运行时数据（arm.db / arm.log / power_backup.json 等） |
| `~/.claude/settings.json` | 已注入 4 个 hooks（备份 settings.arm-backup.json） |

## 四、三信号感知体系（产品核心）

任一信号活跃即保护（宁可误保护，不可漏保护）：

1. **hooks**（精准）：Claude settings.json 注入 4 事件 → `arm hook-ingress` 落库。会丢（S0 下实测 Stop 丢失）→ 有 60s 哨兵自动重注入。
2. **transcript 旁路**（可靠）：扫 `~/.claude/projects/*/*.jsonl` 的 mtime + 尾部 64KB 判忙闲。统一规则：**静默 < 90s = BUSY**（不区分最后是 user 还是 assistant——实测最后=user 但静默 40h 的中断会话必须判空闲）。
3. **进程探测**（兜底）：psutil 扫 claude/codex 进程（按包名/路径精确匹配，勿按 exe 名——ChatGPT.exe 属 MSIX 包 OpenAI.Codex_）。

## 五、血泪教训（踩过的坑，务必遵守）

### Windows/环境层
- **写 VBS/bat 必须 raw string + ASCII + CRLF 字节级写入**：Python `\b`/`\u` 转义陷阱（`\bin`→退格+in、`\uv`→unicode 报错）反复咬人。Write 工具出 LF 也须补 CRLF。
- **FreeConsole 会弄挂 pywebview 消息循环**（窗口完全不出）——勿再试。零黑窗靠 pythonw（GUI 子系统物理无控制台），已三层切换（cli.py 自动转投 / VBS / 计划任务）。
- **arm shim 是 Console 子系统**：arm.exe 双击必弹黑窗，勿用它做 GUI 入口。
- **daemon 运行时锁死 arm.exe**：改代码后必须先停所有 arm 进程再 `uv tool install --force .`，否则装失败且 arm.exe 残缺。
- **Git Bash 会把 powercfg 的 `/x` 参数误转路径** → 一律 subprocess 直调或 PowerShell。
- **GUID 须封装 _GUID 结构体 byref 传** powrprof API；string buffer 静默失败。
- **GetWindowThreadProcessId 在 win32process** 不在 win32gui。
- **单实例锁用 Windows 命名互斥体**（msvcrt 文件锁跨进程失效）。
- **S0 Modern Standby 冻结前台进程**是核心敌人；注册表禁 S0（PlatformAoAcOverride=0）此机型无效已弃；方案=ES_AWAYMODE_REQUIRED + 禁节流 + 电源四项钉 0（快照落盘可恢复，启动时 recover_if_stranded 自愈滞留）。
- **杀 arm 进程只精确匹配 arm 自身特征**（曾误杀 claude.exe——教训深刻）。用户已授权：arm 自己的进程可直接停，不必每次问。

### 代码/UI 层
- **拼 JS onclick 内联引号必崩**（三次"加载中"事故同源）→ 永远用 data-act/data-sid 属性 + 事件委托。
- **server.py 的 do_GET 顺序铁律**：保存 full_path → assets 豁免 → auth（用完整 path，token 在 query 里）→ 剥离 query → 路由。顺序反了会 403/404 错乱（踩过两次）。
- **SQLite WAL 写后必须显式 conn.commit()**（隐式 autocommit 会回滚）。
- agent_state.upsert 的 cwd 用 COALESCE 防止不带 cwd 的事件覆盖丢上下文。
- **状态不依赖退出清理**：daemon 硬杀时 finally 不保证跑 → 靠心跳过期自愈（30s 判失效）。
- pywebview 6.2 create_window 无 icon 参数；app.py 只 create_window 一次（曾循环外+内各建一扇=双窗源）。
- **source_kind=vscode 是 Codex 引擎宿主标记，绝不能当用户入口判据**（ChatGPT Work 模式之谜）。
- 测试绝不写真实 `~/.arm/arm.db`（hook-ingress 有 allow_real 闸门，默认 False）。
- 本机 pytest basetemp 已固定项目内（%TEMP% 权限拒绝）；用 `uv run --with pytest python -m pytest -q` 跑（系统 Python 缺依赖）。
- uv/pip 直连 PyPI 慢，不要随意换镜像源（曾被安全策略拦）。

## 六、当前状态与验证基线

- **102 tests 全过**（改任何代码后跑：`uv run --with pytest python -m pytest -q`）
- 桌面启动终验基线：pythonw 进程 2（uv shim 链=1 实例）、arm.exe 0、conhost 0、ARM 可见窗口 1
- 合盖保活全链路已实测通过（合盖+电池任务跑完，三证据链报告 pass）
- 互护自愈实测：app↔engine 双向拉起（4-6 秒复活）
- hooks 被第三方工具（如 cc-switch）冲掉 → 60s 内自动修复（真机验证过）

## 七、用户工作习惯与偏好（重要）

- **要求每次出问题必须查明真实根因**并说清楚（"目前情况/防护是否开/任务跑时防护是否自动开/是否全自动"）——不要拿表面修复交差。
- **设计克制**："不能太勇于设计，需要才拿"；能复用就复用；不为不存在的需求加抽象。
- **UI/UX 简洁大方**不拘泥条框；UI 术语要在帮助抽屉里有解释。
- 语言：中文交流；CLI 输出避免 unicode 符号（GBK 控制台会炸，用 [OK]/[FAIL]）。
- 交接方式：用户习惯"先文档后动手"，出问题拉 `docs/Self_Healing_Manual.md` 给 Claude 即可迭代，无需重新布线。

## 八、已知的边界与后续方向（仅记录，勿主动开工）

- Codex 深链（codex://）官方未开放会话路由——已预留逻辑，等开放即接入；桌面端跳转已按用户决策移除（独立 APP 点开即达）。
- "继续会话"按钮只对 CLI 会话显示；桌面端会话显示"打开桌面端即见"。云端会话（session_/cse_ 前缀）不能本地 resume。
- Polish_Blueprint 剩余可选项：网络感知 T8（断网告警）、心跳历史图、config.toml、首启引导、主题切换。
- exe 图标定制需 rcedit 改 arm.exe 资源（pywebview 无 icon 参数）。

## 九、马上可以做的事

接手后建议先跑一遍体检确认环境无恙：

```
arm doctor          # hooks/Agent/电源自检
arm status          # 保护状态+会话
uv run --with pytest python -m pytest -q   # 102 全过 = 基线无恙
```

然后等用户提出下一个需求/问题，按手册排障。**不要大规模重构**——三轮打磨的资产都在现有架构上，用户对现状基本满意。
