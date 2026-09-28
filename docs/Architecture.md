> [历史文档] 本文档描述单进程合并（2026-09-26）之前的架构与计划，**现状以 [docs/Self_Healing_Manual.md](Self_Healing_Manual.md) 为准**。

# Agent Runtime Manager — 架构文档（Architecture）v0.2

> 状态：Draft，hooks 机制已确认并回填（§4.3）
> 输入：PRD v1.0 + Research v1.0
> 决策已确认：**语言 = Python**；**感知策略 = 适配器层各取最优**

---

## 0. 架构要回答的问题（来自 Research §9）

| # | 问题 | 本文档答案位置 |
|---|------|----------------|
| 1 | 如何准确判断 Agent 完成？ | §4 Agent 感知层 + §6 状态机 |
| 2 | 如何兼容不同 CLI Agent？ | §4.4 适配器抽象 |
| 3 | 如何处理 Windows Modern Standby？ | §5.3 电源控制 |
| 4 | 网络状态如何影响 Runtime 决策？ | §5.4 + §6 状态机 |

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────┐
│ CLI 层  arm <command>                                 │
│   protect / release / status / watch / doctor / init  │
├──────────────────────────────────────────────────────┤
│ Runtime Engine（决策状态机 · 产品灵魂）                 │
│   订阅 AgentEvent + EnvEvent → 决策 → 下发 PowerAction │
├───────────────────────┬──────────────────────────────┤
│ Agent 感知层           │ 环境感知层                     │
│  adapters/            │  sensors/                     │
│   · Claude (hooks)    │   · power    (电池/AC/电源方案) │
│   · Codex  (proc+out) │   · standby  (合盖/Modern S0)   │
│   · 未来: gemini/aider │   · network  (连通性)           │
├───────────────────────┴──────────────────────────────┤
│ 系统控制层  control/                                   │
│   · execution_state (SetThreadExecutionState)          │
│   · modern_standby  (S0 待机干预)                      │
│   · powercfg 封装                                      │
├──────────────────────────────────────────────────────┤
│ 基础设施  core/                                        │
│   事件总线 · 状态存储(SQLite) · 配置 · 日志 · 单实例锁    │
└──────────────────────────────────────────────────────┘
```

**核心设计原则**：感知与决策解耦。感知层只产"事件"，引擎只消费"事件"并产"动作"，控制层只执行"动作"。三者通过内部事件总线连接，任何一层可单测、可替换。

---

## 2. 技术选型（已定）

| 维度 | 选择 | 理由 |
|------|------|------|
| 语言 | **Python 3.13** | 原型快、Windows 生态成熟、本机现成 |
| 包管理 | **uv** | 本机已装（0.12.10），快、可锁版本、可产单环境 |
| Windows API | **pywin32** | 已验证可用；调 `SetThreadExecutionState` / 电源 / WM_POWERBROADCAST |
| 进程/系统信息 | **psutil** | 已装（7.2.2）；进程检测、CPU、网络连接枚举 |
| CLI 框架 | **Typer** | 类型注解驱动、自带 `--help`、比 argparse 现代（待装） |
| 事件总线 | 标准库 `queue` + 线程 | MVP 不引外部依赖，足够 |
| 状态持久化 | **SQLite**（标准库 `sqlite3`） | 零依赖、事务、`status` 跨进程读取保护状态 |
| 配置 | **TOML**（`tomllib` 读 / `tomli-w` 写） | 标准、人可读，放 `~/.arm/config.toml` |
| 打包分发（后期） | **PyInstaller** 单 exe | 符合"CLI 工具分发给开发者" |
| 测试 | **pytest** | 标准 |

> 待装三方依赖极少：`typer`、`tomli-w`（写配置用）。其余全部标准库或本机已有。

---

## 3. 模块与 Repo 结构

```
agent-runtime-manager/
├── pyproject.toml            # uv 管理，定义 arm 入口点
├── README.md
├── docs/
│   ├── PRD_v1.0_CN.md          # 移入
│   ├── Research_v1.0_CN.md     # 移入
│   └── Architecture.md         # 本文档
├── src/arm/
│   ├── __init__.py
│   ├── __main__.py             # python -m arm
│   ├── cli.py                  # Typer 命令：protect/release/status/watch/doctor/init
│   ├── engine/
│   │   ├── runtime.py          # RuntimeEngine：事件→决策主循环
│   │   ├── state_machine.py    # Agent 状态机 (§6)
│   │   └── policy.py           # 保活策略决策
│   ├── adapters/
│   │   ├── base.py             # AgentAdapter 抽象基类 (§4.4)
│   │   ├── claude.py           # Claude Code：消费 hook 事件→语义状态
│   │   ├── codex.py            # Codex：进程 + exec 输出
│   │   └── hook_ingress.py     # arm hook-ingress：stdin 收 JSON→事件落库
│   ├── sensors/
│   │   ├── base.py             # Sensor 抽象
│   │   ├── power.py            # AC/电池/电量/电源方案 (pywin32)
│   │   ├── standby.py          # 合盖 / Modern Standby 状态
│   │   └── network.py          # 连通性探测
│   ├── control/
│   │   ├── execution_state.py  # SetThreadExecutionState 封装
│   │   ├── modern_standby.py   # S0 干预
│   │   └── powercfg.py         # powercfg 调用封装
│   └── core/
│       ├── events.py           # 事件定义 + 总线
│       ├── store.py            # SQLite 状态读写
│       ├── config.py           # 配置加载
│       ├── logging.py          # 日志
│       └── single_instance.py  # 单实例锁（防止多个 engine 抢电源控制）
└── tests/
    ├── test_state_machine.py
    ├── test_policy.py
    ├── test_adapters.py
    └── conftest.py
```

---

## 4. Agent 感知层（问题一、问题二）

### 4.1 设计：适配器抽象
所有 Agent 统一暴露同一组语义事件，底层机制各自实现：

```python
class AgentEvent(Enum):
    STARTED   = "started"    # 进程/会话启动
    BUSY      = "busy"       # 开始处理一个任务单元
    IDLE      = "idle"       # 完成当前响应，等待用户输入
    FINISHED  = "finished"   # 任务完成（engine 据此判断可释放保护）
    STOPPED   = "stopped"    # 进程/会话退出

class AgentAdapter(ABC):
    name: str
    def detect(self) -> list[AgentInstance]: ...   # 发现正在运行的实例
    def subscribe(self, cb: Callable[[AgentEvent, dict], None]): ...
```

### 4.2 Claude Code 适配器（精准路线 —— hooks，机制已确认）
Claude Code 提供 hook 机制：在特定生命周期点，Claude 执行用户配置的 shell 命令，并把上下文以 **JSON 经 stdin** 传入（详见 §4.3）。

- 配置位置：`%USERPROFILE%\.claude\settings.json`（用户级）或项目级 `.claude/settings.json` 的 `hooks` 字段。
- 我们的做法：`arm init` 往 `settings.json` 注入一组 `command` hook，统一指向 `arm hook-ingress`（轻量入阵子命令），事件落库后由引擎消费。
- 据此获得精准生命周期：`UserPromptSubmit`→BUSY、`Stop`→IDLE、`SessionStart`→STARTED、`SessionEnd`→STOPPED。

### 4.3 Claude hook 机制（已确认）
Claude hook 为 `command` 类型：触发时执行配置的 shell 命令，**事件上下文以 JSON 经 stdin 传入**（不是同名环境变量；另有 `CLAUDE_PROJECT_DIR` 等辅助 env）。

公共字段（多数事件具备）：
```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "permission_mode": "default",
  "hook_event_name": "Stop",
  "stop_hook_active": false
}
```
事件特有字段（示例）：`UserPromptSubmit` 带 `prompt`；`PreToolUse`/`PostToolUse` 带 `tool_name`/`tool_input`/`tool_use_id`；`Notification` 带 `notification_type`/`message`；`SessionStart` 带 `source`；`SessionEnd` 带 `reason`。

**我们用到的事件 → 语义映射：**

| 我们需要的语义 | hook 事件 | 关键字段 | 说明 |
|---|---|---|---|
| 任务单元开始 → BUSY | `UserPromptSubmit` | `session_id`, `cwd`, `prompt` | 用户提交 prompt，agent 开始处理 |
| 一轮响应结束 → IDLE | `Stop` | `session_id`, `stop_hook_active` | **主 agent 完成本轮、交还控制权**；最接近"任务单元结束" |
| 会话开始 → STARTED | `SessionStart` | `session_id`, `cwd`, `source` | |
| 会话结束 → STOPPED | `SessionEnd` | `session_id`, `reason` | 真正的进程/会话退出信号 |
| 需要用户注意 | `Notification` | `notification_type`, `message` | 可用于 `arm status` 提示 |

> **`stop_hook_active` 语义**：当 stop hook 已介入、Claude 处于"因 hook 要求继续"的流程中时为 true。我们据此避免把 hook 导致的继续误判为新一轮用户任务。

**注入方式**：`arm init` 向 `%USERPROFILE%\.claude\settings.json`（用户级）写入：
```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "arm hook-ingress --event UserPromptSubmit" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "arm hook-ingress --event Stop" }] }],
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "arm hook-ingress --event SessionStart" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "arm hook-ingress --event SessionEnd" }] }]
  }
}
```
`arm hook-ingress` 是一个极轻量子命令：从 stdin 读 JSON、附加本地时间戳、写入事件队列（追加到 SQLite 或命名管道），**必须在毫秒级返回**，绝不阻塞 Claude 主流程。命令带 `--event` 显式标注事件类型，与 stdin 里的 `hook_event_name` 交叉校验、互为冗余。

> ⚠️ 已确认的限制：hooks 能可靠区分"本轮响应结束"与"会话结束"，但**无法语义判定用户任务是否真正完成**（`Stop` 只表示 agent 暂停等待输入）。故 FINISHED 仍需 §6.1 的 idle 超时兜底——此取舍与官方能力边界一致。

### 4.4 Codex 适配器（进程 + 结构化输出路线 —— 问题二）
Codex 暂无等价 hooks，采用**进程检测 + 输出/模式推断**：
- `detect`：用 `psutil` 匹配 `codex` 进程树（含 `codex exec` 非交互模式）。
- 状态推断：
  - 存活 → 至少 RUNNING。
  - `codex exec` 非交互模式：进程退出码 + stdout 末尾判断 FINISHED。
  - 交互模式：通过 `codex app-server` / 终端输出启发式判断 busy/idle（MVP 可先粗粒度：进程在=RUNNING、退出=STOPPED）。
- `codex doctor`：在 `arm doctor` 里复用，做环境自检。

> **统一抽象的意义**：引擎层只看到 `AgentEvent`，不关心是 hooks 还是进程探测来的。新增 Agent（gemini/aider）只需新增一个 adapter。

---

## 5. 环境感知 + 系统控制层（问题三、问题四）

### 5.1 电源传感 `sensors/power.py`
- `GetSystemPowerStatus`（pywin32）→ AC/电池、电量百分比、是否充电。
- `powercfg /getactivescheme` → 当前电源方案。
- 事件：`AC_CONNECTED` / `ON_BATTERY` / `BATTERY_LOW`。

### 5.2 待机传感 `sensors/standby.py`
- 监听 `WM_POWERBROADCAST`（`PBT_APMSUSPEND` / `PBT_APMRESUMEAUTOMATIC`）。
- 合盖动作查询：`IOCTL` / `powercfg` 的 LID 动作（GUID `5ca83367-6e45-459f-a27b-476b1d01c936`）。
- 本机实测（2026-09-23）：仅支持 **S0 低功耗待机（联网）**，无 S3。
- **⚠️ 实测修正**：本机 S0 方案的"电源按钮和盖子"子组里**没有"合上盖子"(LIDACTION) 设置项**——S0 设备通常隐藏 LID 动作、由系统接管。因此合盖保活**不能**靠 powercfg 改 LID，须走 §5.3 的 ES_AWAYMODE + 禁进程节流路径（见 control/）。原"对策2 临时改 LID 动作"在 S0 设备上不可行，仅对暴露该设置的 S3 设备适用。

### 5.3 电源控制 `control/`（问题三的核心答案）
- **基础保活**：`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)`。
  - `ES_SYSTEM_REQUIRED`：阻止系统自动睡眠。
  - `ES_AWAYMODE_REQUIRED`：关键——让媒体/ Away Mode 语义下合盖仍可运行。
- **Modern Standby（S0）的真相与对策**：
  - S0 下进程不被冻结，系统进入"联网待机"，但桌面应用可能被电源节流（Power Throttling）。
  - 对策 1：对 Agent 进程用 `PowerSetActiveScheme` + 禁用该进程的 Power Throttling（`PROCESS_POWER_THROTTLING_IGNORE`）。
  - 对策 2（仅 S3 设备）：把合盖动作临时改为"不操作"。⚠️ 实测本机 S0 设备无 LIDACTION 设置项，此对策在 S0 上不可行。
  - 对策 3（S0 关键）：探测 Away Mode / 必要时切"高性能"方案 + 禁目标进程节流。
- **释放**：任务结束后清除 execution state、恢复 LID 动作与电源方案。**这是"不是防睡眠工具"的体现——保护是条件触发的、可逆的。**

### 5.4 网络传感 `sensors/network.py`（问题四）
- 周期性探测：到 LLM API 端点 / 默认网关的连通性（TCP 连接 + 超时）。
- 事件：`NETWORK_UP` / `NETWORK_DOWN` / `NETWORK_FLAKY`。
- **如何影响决策**：引擎把"断网"作为环境信号——Agent 是 API 驱动的，断网时长任务注定失败。MVP 策略：`NETWORK_DOWN` 时 `status` 中明确告警，并在 `watch` 里提示"任务可能因断网停滞"；是否暂停保护由策略配置决定（默认仍保活，因为可能是临时抖动）。

---

## 6. 状态机（引擎核心 —— 问题一最终答案）

### 6.1 Agent 状态（MVP 三态 + 内部细分）
```
            UserPrompt / 检测到进程
                   │
                   ▼
   ┌──────────────────────────┐
   │        RUNNING           │◄──────────────┐
   │   ├─ busy  (处理中)       │               │ 新一轮 prompt
   │   └─ idle  (等输入)       │───────────────┘
   └──────────────────────────┘
        │                │
   进程退出/SessionEnd  任务完成判定
        ▼                ▼
   ┌─────────┐      ┌──────────┐
   │ STOPPED │      │ FINISHED │
   └─────────┘      └──────────┘
```
- **FINISHED vs idle 的判定**（最难的一点）：
  - Claude：`idle` 由 `Stop` 触发。MVP 用**"idle 超时"** 作为 FINISHED 代理——进入 idle 后超过 `finish_grace_seconds`（默认如 120s）无新 prompt，视为本任务单元完成 → 可释放保护。
  - Codex：交互模式同 idle 超时；`exec` 模式进程退出即 FINISHED。
  - 这是 MVP 的务实取舍：不追求理解"任务是否真的做完"，只追求"不再活跃就释放"。

### 6.2 保护（Protection）状态
```
DISARMED ──arm protect──▶ ARMED ──(Agent RUNNING 且 需保活)──▶ PROTECTING
   ▲                                                            │
   └──────────────── Agent FINISHED/STOPPED 且 grace 到期 ───────┘
                        (release)
```

### 6.3 决策矩阵（引擎输入 → 动作）
| Agent 状态 | 电源 | 网络 | 决策动作 |
|---|---|---|---|
| RUNNING/busy | 电池 | up | **进入/维持 PROTECTING**（防合盖睡眠 + 防节流） |
| RUNNING | 电池 | down | 维持保护 + 告警"断网，任务或停滞" |
| RUNNING | AC | up | 维持轻量保护（主要防合盖） |
| idle 超 grace | 任意 | 任意 | → FINISHED，**释放保护**，恢复电源管理 |
| STOPPED | 任意 | 任意 | 释放保护 |
| 无 Agent | 任意 | 任意 | DISARMED，不干预系统 |

---

## 7. 非目标（与 PRD §8 对齐）
不做：GUI Dashboard、Desktop Agent、云端、多 Agent 调度、高级自动恢复、完整任务理解。

## 8. 风险与开放问题
| 风险 | 缓解 |
|---|---|
| Claude hooks 字段/语义与预期不符 | 已确认核心字段（§4.3）；设计只依赖"三语义"，对字段名低耦合 |
| hooks 无法判定任务语义完成 | §6.1 idle 超时兜底，与官方能力边界一致 |
| Codex 交互模式 FINISHED 判定不准 | MVP 用进程+idle 超时，文档标注局限 |
| Modern Standby 下进程仍被节流 | §5.3 对策 1/2 实测验证（本机就是 S0 环境，可直接测） |
| 多实例抢电源控制 | `single_instance.py` 文件锁 |
| 修改用户 `settings.json` 有风险 | `arm init` 前先备份，提供 `arm init --undo` |
