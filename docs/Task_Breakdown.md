> [历史文档] 本文档描述单进程合并（2026-09-26）之前的架构与计划，**现状以 [docs/Self_Healing_Manual.md](Self_Healing_Manual.md) 为准**。

# Agent Runtime Manager — 任务拆解（Task Breakdown）v0.2

> 依据：Architecture v0.2 + PRD §10 验收标准
> 目标：MVP 可验收。每个任务标注 优先级 / 依赖 / 对应验收标准(AC)。
> 进度标记：✅ 已完成 / 🚧 进行中 / ⬜ 未开始

## 验收标准 → 任务映射总览

| PRD §10 验收标准 | 由哪些任务支撑 |
|---|---|
| AC1 Claude Code 可被管理 | T5, T6, T8 |
| AC2 Codex 可被管理 | T7, T8 |
| AC3 用户可开启保护模式 | T9, T11 |
| AC4 合盖+电池下任务持续运行 | T4, T10, T11 |
| AC5 Agent 完成后释放保护 | T8, T11 |
| AC6 用户可查看运行状态 | T9 |

---

## Phase 0 — 工程地基（不直接产出功能，但必须先做）

- **T1 · Repo 脚手架** `P0` ✅
  uv 初始化、`pyproject.toml`、src 布局、Typer 空 CLI、`pytest` 跑通。
  产出：`arm --help` 可用。（已完成：7 命令骨架 + 15 tests 通过）
- **T2 · 基础设施 core** `P0` ✅
  `store.py`（SQLite schema：agent_events/agent_state/protection/env_events 四表，WAL）、`paths.py`、`single_instance.py`（msvcrt 文件锁）。
  注：`config.py`/`logging.py`/`events.py` 待用时再补。
  依赖：T1。（已完成：跨进程读写 + 单实例锁，含测试）

## Phase 1 — 感知能力（问题一/二/四）

- **T3 · 环境传感-电源** `sensors/power.py` `P0` ✅
  AC/电池/电量/方案（pywin32 GetSystemPowerStatus + powercfg），变化才产事件。真机验证：AC/79%/平衡。
- **T4 · 环境传感-待机/合盖** `sensors/standby.py` `P0` ✅
  powercfg /a 解析（区分"上有/没有"两段）；WM_POWERBROADCAST 常量备查。
  ⚠️ 实测重要发现：本机 S0 Modern Standby 方案里**无 LIDACTION 设置项**，合盖保活不能靠 powercfg 改 LID，须走 ES_AWAYMODE + 禁节流。
- **T5 · Claude 适配器** `adapters/claude.py` + `hook_ingress.py` `P0` 🚧
  `arm hook-ingress --event X`（stdin 收 JSON→SQLite），事件映射为 BUSY/IDLE/STARTED/STOPPED。
  已完成：`hook_ingress` 真落库（含 `--selftest` 隔离真实库）；`arm status` 可读。
  待办：事件名映射表抽成 `claude.py` 适配器模块（当前在 hook_ingress 内）。
  依赖：T2。
- **T6 · arm init（hook 注入）** `P0` ✅ 已注入真实 settings.json
  `core/claude_hooks.py`：备份→合并（只动 hooks 键、幂等、保留用户已有 hook、精准移除）→`--undo` 还原。
  arm 已用 `uv tool install` 全局安装（`~/.local/bin/arm.exe` 绝对路径，不依赖 PATH）。
  **已写入真实 `~/.claude/settings.json`**（备份 settings.arm-backup.json），2026-09-23。
  ⚠️ 需重启 Claude Code 生效；当前会话不上报。
  依赖：T5 + arm 全局可调用。
- **T7 · Codex 适配器** `adapters/codex.py` `P1`
  psutil 进程检测 + `codex exec` 输出/退出码判定；交互模式先用"进程在=RUNNING/退出=STOPPED"粗粒度。
  依赖：T2。
- **T8 · 网络传感** `sensors/network.py` `P1`
  TCP 连通性探测（网关 + LLM API 端点），产 NETWORK_UP/DOWN。
  依赖：T2。

## Phase 2 — 决策与控制（问题三、产品灵魂）

- **T9 · Runtime 状态机** `engine/state_machine.py` `P0` ✅
  AgentTracker（busy/idle + idle 超时判 FINISHED）+ ProtectionMachine（DISARMED/ARMED/PROTECTING）+ policy.py 决策矩阵。纯逻辑，含测试。
- **T10 · 电源控制** `control/execution_state.py` + `modern_standby.py` `P0` ✅
  ExecutionStateGuard（ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED，真机验证 enter/exit）；进程防节流 SetProcessInformation（对当前 PID 真机成功）。
- **T11 · Runtime Engine 主循环** `engine/runtime.py` `P0` ✅
  tick()=感知→决策→控制闭环；arm/release/run(daemon,单实例+Ctrl+C 自动释放)。含测试（FakeGuard 验证保活施加/释放）。
  依赖：T3,T4,T5,T9,T10（网络 T8 暂以 network_up=None 占位，不阻塞）。

## Phase 3 — CLI 与验收

- **T12 · CLI 命令集** `cli.py` `P0`
  `protect`（开启保护）/ `release`（释放）/ `status`（看状态）/ `watch`（实时）/ `doctor`（自检，复用 `codex doctor` 思路）/ `init` / `hook-ingress`。
  依赖：T11。
- **T13 · 端到端验收** `P0`
  按 AC1–AC6 逐条真机跑：起 Claude 任务→protect→合盖电池→回来验证运行→完成后验证释放。
  依赖：T12。

---

## 建议实施顺序（关键路径）
```
T1✅ → T2✅ → T5🚧 → T6 → T9✅ → T10✅ → T11✅ → T12 → T13   （Claude 主线）
                 └→ T3✅ → T4✅ ──┘                            （电源/待机）
                 └→ T7 (Codex)、T8 (网络) 随后补 ⬜             （P1，不阻塞主线）
```
**最快可见闭环**：先做通 Claude + 电源保护（AC1/AC3/AC4/AC5/AC6），再补 Codex(AC2) 与网络。

## 剩余工作
- **T5 收尾** 把事件名映射从 hook_ingress 抽成 `adapters/claude.py`（当前内联在 hook_ingress）。
- **T7** Codex 适配器（进程检测 + exec 输出；AC2）。
- **T8** 网络传感（TCP 连通性 → 断网告警；问题四收尾）。
- **T12** CLI 收尾：`watch` 实时刷新；`status` 增加 FINISHED 倒计时。
- **T13** 端到端真机验收：起 Claude 任务 → `arm daemon` → 合盖电池 → 回来验证任务在跑 → 完成后验证自动释放（**需你物理合盖配合**）。

## 实施日志
- **2026-09-23** T1✅ T2✅ T3✅ T4✅ T6✅ T9✅ T10✅ T11✅ T5(落库链路)🚧。70 tests 通过。
  - **产品闭环已在隔离环境真机跑通**：hook→protect(PROTECTING)→status(RUNNING/busy)→Stop→idle→release(DISARMED)。
  - **T6 已真实注入** `~/.claude/settings.json`（备份 settings.arm-backup.json），用户已过目 diff 并确认；arm 全局安装（uv tool）。
  - `arm doctor` 真实自检：Claude/Codex 路径、电源、Modern Standby(S0)。
  - **真机健壮性验证（崩溃场景）**：daemon 启动即 PROTECTING ✓；taskkill /F 硬杀后进程死透、OS 自动释放电源 ✓、状态经心跳过期自愈为 DISARMED ✓。
  - **本阶段修复的真实 bug**：
    1. daemon 启动后默认 DISARMED 空转永不保活 → 改为启动即 `arm()`（用户运行 daemon 的意图就是要保护）。
    2. 单实例锁用 msvcrt 文件锁按 fd 隔离、拦不住第二个 daemon → 改用 Windows 命名互斥体（CreateMutex + ERROR_ALREADY_EXISTS），跨进程可靠。
    3. 测试调 `run()` 默认写真实 `~/.arm/arm.db`，污染生产数据（abc/None 残留）→ 加 `allow_real` 闸门，仅 CLI 生产入口传 True；测试用注入 store。
    4. daemon 硬杀后库里滞留假 PROTECTING → 加**心跳过期机制**（`get_effective_protection`，updated_ts 超 heartbeat_timeout_s=30s 自动判失效），不依赖"退出清理"（硬杀时靠不住）。
    5. daemon 运行时锁死 arm.exe → uv 重装会失败并把 arm.exe 弄残缺。**教训：改代码后必须停 daemon 才能 `uv tool install`**。
  - 踩坑记录：①`%TEMP%\pytest-of-*` 权限异常 → basetemp 固定项目内；②SQLite 写后须显式 `commit()`；③Git Bash(MSYS) 把 powercfg `/x` 误转路径 → subprocess 直调；④本机 S0 方案无 LIDACTION → 合盖保活走 ES_AWAYMODE+禁节流；⑤hook 命令含引号路径 → `_MARKER` 用 `hook-ingress` 识别条目。

## 实施日志（续）
- **2026-09-24 打磨轮**：79 tests 通过。审查发现并修复 6 个问题：
  1. 🔴 僵尸 RUNNING（SessionEnd 漏发→永远保护不释放）→ `_reap_stale_sessions`：busy 30min 无事件→STOPPED
  2. 🔴 idle→FINISHED 没接线（AC5 完成释放失效）→ idle 120s 无事件→FINISHED→释放保护
  3. 🟡 watch 命令 todo → 已实现（终端实时刷新，--interval 可调）
  4. 🟡 UI daemon 心跳借 protection 时间戳误导 → 加独立 `daemon_heartbeat` 表（beat()/heartbeat_age()），UI 判活准确
  5. 🟡 僵尸会话在 UI/watch 显示误导 → 显示侧标注"僵死·等daemon回收"
  6. 🟢 doctor 过时文案（"T10 待实现"）→ 更新
- **新增 `arm ui`**：本地 Web 控制台（127.0.0.1:8620，零依赖标准库 http.server）：状态卡+保护按钮+会话表+事件时间线，2s 自动刷新。合盖测试利器。
- **遗留**：#4 `_active_pids()` 仍为空（S0 禁节流未接真实 PID）——待合盖测试定去留；若 LIDACTION=0 实测有效则可降级，否则走"Agent 跑成后台进程"方案。

## MVP 范围外（明确不做，对应 PRD §8）
GUI、Desktop/Cloud Agent、多 Agent 调度、高级自动恢复、任务语义理解、gemini/aider 等更多适配器。
