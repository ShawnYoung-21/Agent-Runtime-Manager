# 外部调研 Prompt v2：只查四个真正未知的点（Modern Standby × Codex 断网）

> 用途：将代码块完整复制给外部深度研究模型。v1 广谱版见 [External_Research_Prompt_Modern_Standby_Codex.md](External_Research_Prompt_Modern_Standby_Codex.md)（v1 结论质量低，本版只保留未解问题）。
> 日期基准：2026-09-29

```text
# 调研任务：Windows 11 S0 合盖保活的官方机制边界与 Intel AX211 息屏掉线问题（只查四个点）

## 前提（已确认事实，不要重复调研，不要质疑，直接以此为地基）

1. 目标机：ASUS Vivobook S15，Windows 11（26200），`powercfg /a` 仅 S0 Low Power Idle，无 S3。
2. Wi-Fi 网卡：Intel Wi-Fi 6E AX211。
3. 本机合盖动作已设为"不采取任何操作"（lidaction=0，AC/DC 均已设）。
4. ARM 应用在任务运行期间会钉住：无人值守睡眠/睡眠超时/休眠超时 = 永不；电源键 = 不动作；
   并调用 SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED)。
5. 已知（Microsoft 文档）：ES_SYSTEM_REQUIRED 只重置空闲计时器；Away Mode 在 Modern Standby
   系统上不受支持/已弃用；合盖动作由 SUB_BUTTONS LIDACTION 决定而非空闲计时器。
6. 已知候选升级路径：PowerSetRequest(PowerRequestExecutionRequired)（Win10 1709+ 桌面应用可用，
   文档称可让进程在 Modern Standby 期间持续运行）。
7. 已知社区现象：AX211 存在"息屏即掉 Wi-Fi"的多起报告（含 ASUS 机型），常规处置为
   取消网卡"允许计算机关闭此设备以节约电源"、无线适配器省电模式设为最高性能、直装 Intel 驱动。
8. 代码层已修好："ChatGPT.exe 常驻 ≠ 任务活跃"、"全局 WAL 刷新 ≠ 会话活跃"，不再需要调研
   Agent 任务判定问题。

## 只调研以下四个问题

### Q1（P0）：PowerRequestExecutionRequired 在 Win11 S0 桌面应用的可行性

- 官方文档的准确语义：它到底保证什么？（进程不被挂起？网络仍可用？两者？）
- 桌面 Win32 应用（非 UWP、非打包、pythonw 后台进程）调用 PowerSetRequest 的：
  正确用法、需要哪个请求类型组合（SystemRequired? ExecutionRequired? DisplayRequired?）、
  句柄生命周期与释放要求、已知限制。
- OEM 定制（ASUS）是否会削弱它？有无官方或可靠实测证据。
- 与 SetThreadExecutionState 的关系：替代、叠加还是互斥？
- Python (ctypes) 调用 PowerSetRequest 的可行性与示例（powerbase.dll / powrprof.dll？）。

### Q2（P0）：CONNECTIVITYINSTANDBY 隐藏电源设置在本场景的作用

- GUID 与所属子组的准确值（据说为 SUB_NONE 下 F15576E8-98B7-4186-B944-EAFA664402D9，
  请以 Microsoft Learn / ADMX 文档核实）。
- 在 S0 机器上：合盖动作=0、系统未睡眠时，此设置是否根本不参与（只有进入 standby 才生效）？
- 若系统在任务结束后按空闲超时进入 Modern Standby：connected vs disconnected standby
  由什么决定？此设置、网络配置文件（专用/公用）、还是 OEM？
- 它与"任务结束后 ARM 释放保护 → 系统稍后休眠 → 桌面端应用掉线重连"这条路径的关系。

### Q3（P0）：AX211 息屏掉线的 Windows 侧处置有效性

- 以下处置哪些有官方（Microsoft/Intel/ASUS）依据，哪些只是社区经验：
  a. 设备管理器取消"允许计算机关闭此设备以节约电源"
  b. 电源计划"无线适配器设置→省电模式→最高性能"（对应 powercfg 哪个 GUID？
     据说为 SUB_NONE 下 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1，请核实）
  c. 直装 Intel 最新驱动替代 OEM（ASUS）驱动
  d. 网卡高级属性中的 MIMO 省电模式/漫游积极性
- AX211 该问题的 Intel 官方回应或修复版本号（若有）。
- 在"系统完全唤醒、仅息屏"状态下，上述 a/b 是否被证实能阻止掉线？给出证据链。

### Q4（P1）：Codex/ChatGPT Windows 桌面端 Reconnecting 已知问题

- OpenAI 官方渠道（GitHub openai/codex issues、帮助中心、社区）是否有
  "Reconnecting... waiting for network" 的已知问题、修复版本、或官方解释？
- 桌面端与服务端的连接形态（WebSocket/长轮询/HTTP2）与断线自动重连行为，
  有无官方或逆向可证资料。
- 该界面状态是否只影响 UI 显示而不影响本地 codex app-server 继续执行任务？
  有无可验证证据（本地日志字段、任务完成落盘等）。

## 产出要求（违反任意一条即视为不合格）

1. 每条结论标注证据等级：A=Microsoft/Intel/ASUS/OpenAI 官方文档或官方 issue（附链接）；
   B=可复现实验/源码；C=多个独立社区实测；D=推测（须明标）。
2. 给出全部来源链接；给不出链接的写"未找到公开来源"。禁止编造 GUID、API、注册表项、issue。
3. Q1/Q2 的 GUID、API 名、取值语义必须以 Microsoft Learn 原文为准，逐字引用关键句（附链接）。
4. 最后给一节"对 ARM 的落地建议"：只回答
   (a) 是否应该把 SetThreadExecutionState 升级/叠加为 PowerSetRequest(ExecutionRequired)；
   (b) 合盖实测应优先验证哪些网卡设置（按证据等级排序）；
   (c) 任务结束释放后系统进入 Modern Standby 导致桌面端重连——这是否符合设计预期，无需处理。
   每条建议附证据等级。
5. 中文输出。

## 产出落盘（必须执行）

调研完成后，把完整报告写入以下文件（Markdown 格式，路径含空格，写入时保留原样）：

    C:\Users\yangchaoxin\Desktop\Agent Runtime Manager\.claude\worktrees\magical-blackwell-95d182\docs\ARM_Modern_Standby_Research_Q1-Q4_Report.md

要求：

1. 只创建/覆盖这一个文件，不得改动工作区内任何其他文件。
2. 报告结构固定为：
   一、结论摘要（≤500 字）
   二、Q1：PowerRequestExecutionRequired
   三、Q2：CONNECTIVITYINSTANDBY
   四、Q3：AX211 息屏掉线处置
   五、Q4：Codex 桌面端 Reconnecting
   六、对 ARM 的落地建议（a/b/c 三条，各附证据等级）
   七、来源链接汇总
   八、事实 / 推测 / 未找到公开来源 项清单
3. 若你的运行环境无法访问该路径，则把完整报告全文放在你的最终回复里输出，
   并在开头注明"未能写入文件"。

## 不需要调研（避免发散）

- 不要再解释 Modern Standby 与 S3 的区别、SetThreadExecutionState 基础语义（前提已给）。
- 不要调研 Claude Code、ARM 架构、任务判定、防睡眠工具对比。
- 不要给"改高性能电源计划/禁睡眠"这类已被前提覆盖的建议。
```

## 使用说明

1. 复制上面代码块全文给外部模型，并确保它的运行目录/可访问范围包含本工作区
   （`...\Agent Runtime Manager\.claude\worktrees\magical-blackwell-95d182`），
   这样它才能按"产出落盘"要求把报告写进 `docs\ARM_Modern_Standby_Research_Q1-Q4_Report.md`。
2. 它跑完后，直接告诉我"报告写好了"，我从工作区读取该文件开始核对。
3. 核对要点：GUID/API 引用是否与 Microsoft Learn 原文一致、证据等级是否真实（点开链接抽查）。
   若它仍不给链接/证据等级，直接判废重跑，不要人工替它补全。
4. 核对通过后再分两路落地：代码路（PowerSetRequest 升级）与系统路（网卡设置，须先登记台账）。
