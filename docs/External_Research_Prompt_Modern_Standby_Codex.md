# 外部调研 Prompt：Windows 11 Modern Standby 合盖电池场景下 Codex/ChatGPT 桌面端断网重连

> 用途：将下文完整复制给外部深度研究模型，调研结果回来后，再结合 ARM 代码与本机实测落地。  
> 日期基准：2026-09-28

```text
# 调研任务：Windows 11 Modern Standby 合盖电池场景下 Codex/ChatGPT 桌面端断网重连问题的根因与可靠解决方案

## 背景

我正在开发一个 Windows 11 本地 AI Agent 运行时管理器 ARM（Agent Runtime Manager）。它的目标是：电脑合盖、使用电池时，Claude Code / Codex 的真实任务继续运行；任务完成后自动释放保活，不把“桌面应用仍开着”误认为“任务仍在执行”。

目标机器是 Windows 11 笔记本，使用 Modern Standby / S0 Low Power Idle，不能使用传统 S3 睡眠。ARM 当前的保活链路包括：

1. 引擎线程周期运行；
2. 使用 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)`；
3. 临时把睡眠、无人值守睡眠、休眠超时设置为永不，并在结束后通过快照恢复；
4. 对目标 Agent 进程做电源节流控制；
5. 通过 Claude hooks、Claude transcript、Codex catalog/timeline/rollout 和进程探测判断 Agent 是否正在工作；
6. 网络状态只用于提示，不应该因为短暂断网而释放真实任务的保护。

实际机器上已经确认：

- `powercfg /a` 显示只有 S0 Low Power Idle Connected Standby，没有 S3；
- Codex Windows 桌面应用的进程名是 `ChatGPT.exe`，路径类似：`C:\Program Files\WindowsApps\OpenAI.Codex_...\app\ChatGPT.exe`；
- Codex 还有常驻的 `codex.exe ... app-server` 进程；
- Codex 使用 `~/.codex/sqlite/codex-dev.db`、`thread_timeline_ledger`、`local_thread_catalog`、`logs_2.sqlite` 等本地数据；
- `logs_2.sqlite-wal` 在桌面应用常驻、后台重连或其它连接活动时可能持续刷新，因此不能把全局 WAL mtime 当成某个会话正在执行任务的证据；
- ARM 已经修正了一个误判：不再因为 ChatGPT.exe 常驻或全局 WAL 刷新，就把最近的 Codex 会话判定为 busy。

## 我要解决的问题

真实实测场景：

1. 在 Codex/ChatGPT 桌面端启动一个任务；
2. 确认任务开始后拔掉电源；
3. 合上笔记本盖子；
4. 回来后发现桌面端界面反复显示：

   `Reconnecting... waiting for network`

同时需要判断：

- 这是 Windows Modern Standby 下 Wi-Fi/网络连接被挂起；
- 还是网卡从 D0 进入 D3；
- 还是 Connected Standby 实际变成了 Disconnected Standby；
- 还是 Electron/Chromium 的网络服务、WebSocket、HTTP/2、代理/VPN 链路在合盖后断开；
- 还是 Codex app-server 与桌面 UI 之间的本地连接断开；
- 还是 Codex 服务端连接仍在，但 UI 渲染层或网络状态机没有恢复；
- 是否与 `SetThreadExecutionState`、Away Mode、电源计划、Power Throttling 或 Wi-Fi 驱动策略冲突。

我要的不是“关闭省电/改高性能电源计划”这种未经验证的泛泛建议，而是能够回答：

> 在 S0 Modern Standby 合盖+电池场景下，如何让 Codex/ChatGPT 桌面端的任务连接尽可能稳定；如果无法保证，ARM 应该如何准确检测、告警和恢复，且不能误把常驻桌面进程当作任务活跃？

## 我的当前方案（待验证）

### ARM 当前保活方案

ARM 目前已经使用：

```text
SetThreadExecutionState(
    ES_CONTINUOUS |
    ES_SYSTEM_REQUIRED |
    ES_AWAYMODE_REQUIRED
)
```

另外临时钉住：

- `UNATTENDSLEEP`
- `STANDBYIDLE`
- `HIBERNATEIDLE`

为永不，并在保护结束时恢复原始快照。

ARM 不主动钉住 `VIDEOIDLE`，允许屏幕按用户配置熄灭。

ARM 还会尝试对 Agent 进程禁用 Power Throttling。

### ARM 当前任务识别方案

Claude：

- hooks；
- transcript mtime 和末条消息；
- Claude Code 进程作为兜底。

Codex：

- `local_thread_catalog`；
- `thread_timeline_ledger`；
- rollout JSONL；
- `codex-command-runner.exe` 或 CLI 进程；
- 不再把 `ChatGPT.exe` 常驻作为任务活跃；
- 不再把全局 `logs_2.sqlite-wal` mtime 归因给最近会话；
- 会话级 timeline 终态优先：
  - `session-ended`
  - `voice-work-terminal status=completed`
  - `voice-work-terminal status=interrupted`
- 没有会话级终态时，才根据该会话自身 `source_updated_at` 的静默阈值判断。

### ARM 当前网络方案

计划使用轻量网络探测：

- TCP 探测默认网关、代理或可配置目标；
- 短超时；
- 结果缓存；
- 网络 down 时在 UI 和日志中提示；
- 网络 down 不自动撤销真实任务的保活。

请重点审查这个方案是否合理，尤其是：

- TCP 探测目标应该是什么；
- 默认网关、DNS、代理、本地 Codex 服务端、OpenAI API 之间应该如何分层判断；
- “网络可达”与“Codex API 可用”如何区分；
- 是否需要使用 Windows WLAN API、Network List Manager、PowerShell、ETW 或 `netsh`；
- 是否应该增加应用级重连/恢复动作。

## 我要你调研的具体问题（按优先级）

### P0：最关键问题——合盖后断网的真实根因和最小可靠修复

请围绕“Windows 11 S0 Modern Standby + 合盖 + 电池 + Codex/ChatGPT 桌面端出现 `Reconnecting... waiting for network`”建立证据链，回答：

1. Windows 11 Modern Standby 在合盖后，对以下对象的实际处理分别是什么：
   - Wi-Fi 网卡；
   - TCP 连接；
   - DNS；
   - 代理/VPN；
   - Electron/Chromium 网络进程；
   - 本地 `codex.exe app-server`；
   - 桌面 UI 与远端 API 的 WebSocket/HTTP 连接。
2. `ES_SYSTEM_REQUIRED` 与 `ES_AWAYMODE_REQUIRED` 在 S0 设备上到底能保证什么，不能保证什么？
3. 将睡眠、无人值守睡眠、休眠超时设置为 0，是否能保证：
   - Wi-Fi 不断；
   - 网卡不进入 D3；
   - TCP/WebSocket 不断；
   - Electron 网络服务不被挂起？
4. `SetThreadExecutionState` 是否只影响睡眠决策，而不影响 Connected Standby 下的网络保活和驱动电源状态？
5. Windows 的 Connected Standby / Disconnected Standby 机制，是否允许普通桌面 Win32/Electron 应用在合盖电池时保持持续联网？
6. 是否存在官方支持的 API、系统设置、组策略或电源策略，可以让特定应用在合盖 S0 期间保持联网？
7. 是否存在硬件/固件/网卡驱动限制，使软件无法可靠解决？如果存在，请明确指出“不可保证”的边界。
8. 对 ARM 来说，最小、最可靠的修复应该是：
   - 继续优化电源 API；
   - 让 Codex 任务变成 Windows 后台活动/Modern Standby 允许的活动；
   - 调整 Wi-Fi/网络适配器电源策略；
   - 让 Codex/app-server 自动恢复连接；
   - 还是只能做“断连检测 + 任务状态保护 + 恢复提示”？

请给出“可以确认的事实”和“推测”，不能混写。

海外重点查：

- Microsoft Learn 关于 Modern Standby / S0 Low Power Idle；
- Windows Connected Standby 与 Disconnected Standby；
- `SetThreadExecutionState` 官方文档；
- Windows Power Throttling；
- Network connectivity during Modern Standby；
- Wi-Fi D0/D3、NDIS、NetAdapter power management；
- Windows App SDK / Win32 background activity；
- Electron/Chromium 网络进程在 Windows suspend/resume 下的行为；
- OpenAI Codex Windows 桌面端官方文档、GitHub issue、官方支持渠道；
- Chromium/Electron suspend/resume、WebSocket、network service 相关 issue。

国内重点查：

- 微软中国官方文档和 Windows 技术社区；
- Intel Wi-Fi 6E AX211 在 Windows 11 Modern Standby 下的已知问题；
- 华硕、联想、戴尔、惠普等厂商关于 S0 合盖断网/Modern Standby/Wi-Fi 电源管理的官方支持文档；
- 国内对 Connected Standby、S0ix、D0/D3、合盖断网问题的实测文章，但必须区分厂商官方资料和个人经验。

### P1：可落地的诊断、检测和恢复方案

请设计一套不依赖猜测的诊断实验矩阵，用来区分以下根因：

1. Windows 整体网络断开；
2. Wi-Fi 网卡进入低功耗或 D3；
3. DNS 失败但 TCP 仍可用；
4. 默认网关可达但公网不可达；
5. 代理/VPN 断开；
6. OpenAI API 不可达；
7. Codex app-server 本地进程仍活着但桌面 UI 连接断开；
8. Electron/Chromium 网络服务没有恢复；
9. Codex 服务端会话状态丢失；
10. ARM 的保活方式与网络驱动策略冲突。

请给出每个实验的：

- 前置条件；
- 操作步骤；
- 要采集的命令或日志；
- 预期结果；
- 如何解释结果；
- 是否需要管理员权限；
- 是否会改系统；
- 如何恢复原状。

优先考虑只读诊断：

- `powercfg /a`
- `powercfg /requests`
- `powercfg /sleepstudy`
- `powercfg /systemsleepdiagnostics`
- `powercfg /systempowerreport`
- `powercfg /energy`
- `powercfg /query`
- `powercfg /getactivescheme`
- `netsh wlan show interfaces`
- `netsh wlan show drivers`
- `Get-NetAdapter`
- `Get-NetAdapterPowerManagement`
- `Get-NetAdapterAdvancedProperty`
- `Get-NetIPConfiguration`
- `Get-DnsClientServerAddress`
- `Get-NetConnectionProfile`
- `Get-WinEvent`
- WLAN-AutoConfig 事件；
- Kernel-Power 事件；
- NDIS 事件；
- TCP/IP 事件；
- Windows 网络连接状态；
- Codex `logs_2.sqlite`；
- Codex `codex-dev.db`；
- Codex app-server 标准输出或日志；
- Electron/Chromium 日志（如果有官方支持的启动参数或日志位置）。

请特别说明：

- 哪些指标能证明“系统断网”；
- 哪些指标只能证明“Codex 应用自身重连”；
- 如何区分桌面 UI 断线和 Agent 任务本身中断；
- 如何判断任务实际上已经完成，只是 UI 一直显示 reconnecting；
- 如何判断任务仍在运行但连接暂时断开。

请设计至少四组对照实验：

1. 开盖 + 电池；
2. 合盖 + 电池；
3. 合盖 + 插电；
4. 开盖 + 插电。

最好再增加：

5. 合盖 + 电池 + ARM 保活开启；
6. 合盖 + 电池 + ARM 保活关闭；
7. 合盖 + 电池 + Wi-Fi；
8. 合盖 + 电池 + 有线网络或 USB 网卡；
9. 合盖 + 电池 + VPN；
10. 合盖 + 电池 + 代理。

### P1：无需管理员和需要管理员的解决方案对比

请把所有建议分成以下类别：

#### A. 无需管理员、低风险、可随时撤销

例如：

- 应用级连接探测；
- Codex/app-server 重新连接；
- Electron 窗口恢复或重新加载；
- 网络变化后重建 WebSocket；
- 任务状态与连接状态分离；
- UI 告警；
- 记录断连期间的 Agent 任务证据；
- 只读检测和诊断日志。

#### B. 需要管理员、可逆但影响系统电源/网卡

例如：

- 网卡允许计算机关闭此设备以节约电源；
- Wi-Fi 适配器高级属性；
- 电源计划；
- Modern Standby 相关设置；
- 组策略；
- 网络适配器电源管理；
- 禁止某些电源节流；
- Windows 服务或计划任务。

#### C. 高风险、不建议默认启用

例如：

- 注册表强制禁用 Modern Standby；
- `PlatformAoAcOverride`；
- 强行切换 S0/S3；
- 永久改电源计划；
- 禁用网卡节能；
- 持续保持系统完全唤醒；
- 模拟鼠标/键盘阻止睡眠；
- 关闭 Windows 电源管理；
- 依赖第三方防睡眠工具。

对每个方案请给：

- 适用前提；
- 是否适用于 S0；
- 是否适用于本机 ASUS Vivobook S15 + Windows 11；
- 是否需要管理员；
- 是否需要重启；
- 对电池续航的影响；
- 对发热和睡眠的影响；
- 对其他应用的副作用；
- 失败时如何恢复；
- 是否得到 Microsoft/Intel/厂商官方支持；
- 证据等级。

### P1：ARM 应该如何设计“断网但任务可能仍在运行”的状态机

请审查并改进以下状态模型：

```text
TASK_ACTIVE
TASK_FINISHED
APP_ONLINE_IDLE
APP_RECONNECTING
NETWORK_DOWN
TASK_STATE_UNKNOWN
```

要求：

1. 不能因为 `ChatGPT.exe` 存在就判定 TASK_ACTIVE；
2. 不能因为全局 `logs_2.sqlite-wal` 刷新就判定某个会话 TASK_ACTIVE；
3. 网络断开时，不能立即释放真实任务的保活；
4. 任务完成的证据应优先于 UI 是否显示 reconnecting；
5. 必须区分：
   - Agent 任务是否仍执行；
   - Codex app-server 是否运行；
   - UI 是否连接；
   - 外网是否可达；
   - Wi-Fi 是否连接；
   - ARM 是否正在保护。
6. 给出合理的超时、滞后和恢复策略；
7. 给出状态转换图或伪代码；
8. 说明如何防止“断网导致永久保护不释放”；
9. 说明如何防止“误释放导致合盖后任务被系统暂停”。

请重点判断以下策略是否合理：

```text
网络断开 != 任务结束
UI reconnecting != 任务结束
ChatGPT.exe 常驻 != 任务活跃
全局 WAL 活跃 != 某个会话活跃
明确 session-ended/completed/interrupted > 自身会话静默 > 常驻进程
```

### P2：行业和学术备选方向

请查找是否有关于以下问题的公开研究、工程实践或官方设计：

- Modern Standby 下 AI Agent/后台开发工具的可靠运行；
- Windows S0ix 下长任务与网络保活；
- Electron 应用 suspend/resume 网络恢复；
- Chromium Network Service 在系统睡眠和网络变化后的连接恢复；
- WebSocket/HTTP2 在 Wi-Fi 电源状态变化后的恢复；
- NDIS 网卡 D0/D3 状态与后台应用；
- Windows Background Activity Moderator；
- Windows Connected Standby 网络策略；
- IDE/远程开发工具在合盖场景的保活；
- VS Code Remote、JetBrains Gateway、Microsoft Dev Box、GitHub Codespaces、远程终端在睡眠/断网后的恢复策略；
- OpenAI Codex、Claude Code、Cursor、Cline、Continue 等 Agent 工具在桌面端/CLI 任务完成判定和断线恢复方面的公开实现。

英文关键词至少包括：

```text
Windows 11 Modern Standby Wi-Fi disconnect lid close battery
S0 Low Power Idle network connectivity Wi-Fi D3
Connected Standby disconnected standby network policy
SetThreadExecutionState Away Mode Modern Standby limitations
Windows power throttling network adapter D0 D3
Electron Chromium suspend resume reconnect WebSocket Windows
Codex desktop Reconnecting waiting for network
OpenAI Codex Windows desktop network reconnect
Windows 11 lid close Wi-Fi disconnect Intel AX211
```

中文关键词至少包括：

```text
Windows 11 合盖断网 Modern Standby
S0 低功耗待机 Wi-Fi D3
联网待机 断网待机
合盖后无线网卡断开
Intel AX211 合盖断网
Windows 11 合盖 Electron 重连
Codex 桌面端 Reconnecting waiting for network
```

## 产出要求

1. **结论先行**：先用不超过 500 字回答：
   - 本问题最可能的根因是什么；
   - ARM 当前保活方案解决了什么；
   - ARM 当前保活方案没有解决什么；
   - 是否存在能让普通桌面 Codex 应用在所有 S0 硬件上合盖不断网的可靠通用方案；
   - 最推荐的短期、中期、长期方案分别是什么。
2. 必须明确区分：
   - “系统不睡眠”；
   - “进程没有退出”；
   - “Wi-Fi 仍连接”；
   - “TCP 仍可用”；
   - “Codex app-server 仍运行”；
   - “Codex UI 仍连接”；
   - “Agent 任务仍执行”；
   - “任务结果已经完成并落盘”。
3. 所有结论必须标注证据级别：
   - A：Microsoft/Intel/设备厂商/OpenAI 官方文档或官方 issue；
   - B：可复现实验、源码、可靠工程文档；
   - C：多个独立实测但缺乏官方确认；
   - D：合理推测，不能当作事实。
4. 不要编造库、API、注册表项、组策略、论文、Codex 功能或 issue。给不出可验证链接的内容，必须标记为“未找到公开来源”。
5. 每条重要事实都要给来源链接：
   - Microsoft Learn / Microsoft Support；
   - Intel 官方文档；
   - ASUS/Lenovo/Dell/HP 官方文档；
   - Electron/Chromium 官方 issue 或文档；
   - OpenAI 官方文档、官方 GitHub 或官方支持页面；
   - 论文使用 arXiv、会议官网、期刊官网链接；
   - 第三方文章只能作为补充，并标明不是官方证据。
6. 分清“已验证事实”和“推测”，分别列出，不要把经验判断写成官方行为。
7. 海内外分述：
   - P0/P1/P2 各一节；
   - 每节内都分“海外”和“海内”；
   - 海内重点关注 Microsoft 中国、Intel 中国、华硕/联想/戴尔/惠普中国支持文档和国内工程实测；
   - 海外重点关注 Microsoft Learn、Intel 官方、Electron/Chromium、OpenAI 官方资料。
8. 必须专门评估我的当前方案：
   - `SetThreadExecutionState + Away Mode` 是否继续保留；
   - 睡眠/休眠超时钉 0 是否继续保留；
   - 是否应该调整为应用级断线检测和恢复；
   - 是否应该加入 WLAN/网卡状态传感器；
   - 是否应该加入 Codex app-server 健康检查；
   - 是否应该引入 Windows 事件日志和 SleepStudy；
   - 是否应该修改网卡省电设置；
   - 是否应该修改注册表或组策略；
   - 哪些改动不应该默认启用。
9. 请给一张明确的方案对比表：

| 方案 | 是否解决系统睡眠 | 是否解决 Wi-Fi 断开 | 是否解决 Codex 重连 | 是否需管理员 | 电池影响 | 风险 | 可逆性 | 推荐级别 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
10. 请给出 ARM 推荐的最终架构，包括：
    - 传感器；
    - 状态机；
    - 超时；
    - 日志；
    - UI；
    - 任务完成判定；
    - 网络断连后的恢复策略；
    - 永久保护泄漏的兜底策略。
11. 请给出可直接实施的诊断实验矩阵，不要只讲理论。
12. 请把每个 Windows 命令标明：
    - 是否只读；
    - 是否需要管理员；
    - Git Bash 下是否可能吃掉参数；
    - PowerShell 或 Python `subprocess` 的推荐写法；
    - 如何恢复改动。
13. 最后必须给出：
    - “我现在应该先做什么”；
    - “暂时不要做什么”；
    - “需要我下一次真实合盖实测采集什么证据”；
    - “如果仍然失败，如何根据证据决定是改 ARM、改网卡、改 Codex，还是接受硬件边界”。

## 建议的最终报告结构

```text
一、结论摘要
二、现象拆解：Reconnecting 到底说明了哪一层断了
三、Windows Modern Standby 的事实边界
四、Codex/ChatGPT 桌面端与 app-server 的连接链路
五、P0：最可能根因（海外 / 海内）
六、P1：诊断实验矩阵（含命令和预期结果）
七、P1：可落地方案分级（无需管理员 / 需管理员 / 不建议）
八、P1：ARM 状态机与防止永久保护泄漏
九、P2：行业实践、开源实现与学术资料
十、对当前 ARM 方案的逐项判断
十一、最终推荐架构
十二、下一轮真实合盖验收清单
十三、来源链接汇总
十四、事实 / 推测 / 未知项清单
```

## 不需要调研的内容（避免发散）

1. 不要把常驻 `ChatGPT.exe`、`codex.exe app-server` 或全局 `logs_2.sqlite-wal` 刷新直接当成任务活跃证据；这部分已经确认不可靠，重点是研究正确的会话级和系统级证据。
2. 不要泛泛介绍“什么是防睡眠软件”或重新设计 ARM 的基本二态模型；ARM 的目标已经确定为“任务运行时保护，任务结束后释放”。
3. 不要优先推荐第三方鼠标抖动、防睡眠工具、永久高性能模式或永久禁用睡眠；除非有明确证据证明它们解决的是本问题中的某一具体层，并且要说明代价、权限和可逆性。
4. 不要把“关闭 Modern Standby、强制切换 S3、修改 `PlatformAoAcOverride`”当作默认方案；只能作为高风险实验单独评估，必须给出硬件适用性、Windows 版本限制、恢复方式和官方证据。
5. 不要把网络 down 自动等价为任务结束；也不要把 UI 显示 reconnecting 自动等价为 Agent 任务已经停止。
6. 不要只给命令清单而没有“执行后如何解释结果”的判断规则。
7. 不要只引用论坛单一帖子或未经验证的 AI 生成内容作为结论依据。

请记住：这个调研的最终目标不是写一篇 Windows 科普，而是帮助我把 ARM 改造成一个在真实 Windows 11 S0 合盖电池场景下“尽量不误判、不漏保护、能解释失败原因、任务结束能释放”的可靠运行时管理器。
```

## 使用说明

1. 将代码块中的完整 Prompt 复制给外部深度研究模型。
2. 要求外部模型保留所有来源链接、证据等级和实验解释。
3. 将调研结果保存或直接贴回本项目会话。
4. 再根据结果决定是否修改 ARM、网卡设置或 Codex 侧恢复策略。

本文件只记录调研任务，不会自动修改 Windows 电源、网卡、注册表、计划任务或 ARM 全局安装状态。
