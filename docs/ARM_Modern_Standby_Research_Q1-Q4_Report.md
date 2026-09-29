# ARM Modern Standby 调研报告：Q1–Q4

调研日期：2026-09-29。适用前提：提示词给定的 ASUS Vivobook S15、Windows 11 build 26200、S0 Low Power Idle、Intel AX211，以及 ARM 当前的合盖和任务期电源配置。本文没有在目标机上做合盖实验，也没有更改系统设置。证据等级：**A**＝厂商正式文档、产品公告或厂商发布的源码；**B**＝可检查的源码、日志或可复现实验（须说明复现范围）；**C**＝多个独立用户实测；**D**＝机制推断或未经复现的单一报告。托管在官方 GitHub 仓库的用户 issue 只证明“有人报告”，不自动等于官方确认或 A 级修复结论。

## 一、结论摘要（≤500 字）

**Q1 [A]** `ExecutionRequired` 只保护调用进程，不保证 Wi‑Fi 或 `ChatGPT.exe`；S0 电池供电时，请求在睡眠超时届满五分钟后终止。可与 `SystemRequired` 做任务期叠加实验，不能当作断网修复。**Q2 [A]** `CONNECTIVITYINSTANDBY` 旧电源项属 `SUB_NONE`，Windows 10 2004 起弃用；Win11 仍列出同 GUID 的 AC/DC 管理策略。它们只针对待机期间。**Q3 [A]** Intel 明确说“允许关闭设备”复选框不影响正常 Wi‑Fi 省电；优先核对无线电源模式和驱动，未找到 AX211 息屏掉线的指定修复版本。**Q4 [A/D]** 本地桌面 App 与 app-server 使用 stdio；公开 issue 有同款重连提示，但无官方修复公告。仅凭提示无法判断任务是否完成。

## 二、Q1：PowerRequestExecutionRequired

### 1. 官方语义和边界

- **[A]** Microsoft 对该请求的原文关键句为：“The calling process continues to run instead of being suspended or terminated by process lifetime management mechanisms.” 这里限定的是 **calling process**，不是整机、网卡或其他进程；允许运行多久还受 OS 和电源策略控制。`PowerRequestSystemRequired` 的语义是阻止因用户无操作而自动睡眠，`PowerRequestDisplayRequired` 会保持屏幕亮起，`AwayModeRequired` 只适用于传统 S3。[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)
- **[A]** Microsoft 还明确写道：“On Modern Standby systems on DC power, power requests are terminated after 5 minutes.” 另一个 API 页面把起算点细化为**系统睡眠超时届满后五分钟**；用户主动按电源键、合盖触发睡眠或菜单选择睡眠时，普通请求会终止。目标机合盖动作是“不采取任何操作”，但需要实测是否确实没有其他路径进入待机。[Microsoft `POWER_REQUEST_TYPE`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_power_request_type)、[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)
- **[A]** Microsoft 的 Modern Standby 文档指出，DAM 阶段完成后会暂停桌面应用；待机联网文档又指出第三方桌面应用/服务不应依赖 Modern Standby 期间的网络访问。因此不能从 `ExecutionRequired` 推出“AX211 不会断线”或“Codex 一定能联网继续跑”。[Microsoft 应用集成](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/integrating-apps-with-modern-standby)、[Microsoft 待机网络电源管理](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/networking-power-management-for-modern-standby-platforms)
- **[A]** Win32 桌面应用可调用该 API；没有 Microsoft 文档表明 Python `pythonw.exe` 或未打包 Win32 进程被排除。`PowerRequestExecutionRequired` 只能由**应用**使用，服务不能使用。ARM 进程自身调用只能保护 ARM 进程，不能替 `ChatGPT.exe` 创建它的进程级执行请求。[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)、[Microsoft `PowerClearRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powerclearrequest)

### 2. 组合、生命周期和与现有调用的关系

- **[A/D]** 若目标是黑屏时保持 ARM 自身执行，`ExecutionRequired` 是对应类型；若还要明确阻止自动睡眠，则同时申请 `SystemRequired` 更稳妥。原因是 `PowerSetRequest` 说明**只在 S3** 写明 `ExecutionRequired` 隐含 `SystemRequired`，而 `PowerClearRequest` 页面泛称前者隐含后者；两页表述范围不同，不能据此断言 Win11 S0 单独申请 `ExecutionRequired` 一定足够。`DisplayRequired` 与黑屏目标相反，不需要；`AwayModeRequired` 对 S0 不适用。此“同时申请”的建议是依据语义作出的保守实现判断，具体效果须在目标机验证。[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)、[Microsoft `PowerClearRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powerclearrequest)
- **[A]** 用 `PowerCreateRequest` 创建句柄，任务开始前对每一种类型各调用一次 `PowerSetRequest`；结束时相应调用 `PowerClearRequest` 递减计数，最后 `CloseHandle`。失败时读取 `GetLastError`。理由字符串应说明任务原因。若多次 `Set`，须匹配次数的 `Clear`。这些函数在 **Kernel32.dll**，不是 `powerbase.dll`/`powrprof.dll` 的这个调用入口。[Microsoft `PowerCreateRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powercreaterequest)、[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)、[Microsoft `PowerClearRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powerclearrequest)、[Microsoft `REASON_CONTEXT`](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-reason_context)
- **[D]** Microsoft 没有给出 `PowerSetRequest` 与 `SetThreadExecutionState` 互斥的说明；可在实验阶段保留现有调用并叠加新请求，按各自规则释放。它们语义不同，不应把新 API 当作对现有调用的无条件替代。未找到 ASUS Vivobook S15/AX211 上 OEM 电源策略削弱此请求的官方说明或可信复现实测；“OEM 会削弱”仍属未证实。Microsoft 文档已列出 OS/策略与 DC 截止条件，无须另假设 OEM 干预。[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)

### 3. Python `ctypes` 调用示例（接口示意，未在目标机实测）

**[B：按 Microsoft ABI 定义编写、尚未在目标机执行]** 下例保持屏幕可熄灭，并为**运行此代码的 Python 进程**申请 `SystemRequired`（枚举 1）与 `ExecutionRequired`（枚举 3）。结构字段、简单字符串标志和 API 签名依据 Microsoft 文档；`POWER_REQUEST_CONTEXT_VERSION=0` 与枚举数值应在实际构建环境的 Windows SDK 头文件复核。[Microsoft `REASON_CONTEXT`](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-reason_context)、[Microsoft `POWER_REQUEST_TYPE`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_power_request_type)、[Microsoft `PowerCreateRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powercreaterequest)

```python
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager

class DETAIL(C.Structure):
    _fields_ = [("LocalizedReasonModule", W.HMODULE),
                ("LocalizedReasonId", W.ULONG),
                ("ReasonStringCount", W.ULONG),
                ("ReasonStrings", C.POINTER(W.LPWSTR))]

class REASON(C.Union):
    _fields_ = [("Detailed", DETAIL), ("SimpleReasonString", W.LPWSTR)]

class REASON_CONTEXT(C.Structure):
    _fields_ = [("Version", W.ULONG), ("Flags", W.DWORD), ("Reason", REASON)]

k32 = C.WinDLL("kernel32", use_last_error=True)
k32.PowerCreateRequest.argtypes = [C.POINTER(REASON_CONTEXT)]
k32.PowerCreateRequest.restype = W.HANDLE
k32.PowerSetRequest.argtypes = [W.HANDLE, C.c_int]
k32.PowerSetRequest.restype = W.BOOL
k32.PowerClearRequest.argtypes = [W.HANDLE, C.c_int]
k32.PowerClearRequest.restype = W.BOOL
k32.CloseHandle.argtypes = [W.HANDLE]
k32.CloseHandle.restype = W.BOOL

@contextmanager
def task_power_request():
    reason = "ARM task is running"
    ctx = REASON_CONTEXT(0, 1, REASON(SimpleReasonString=reason))
    handle = k32.PowerCreateRequest(C.byref(ctx))
    if not handle or handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    active = []
    try:
        for request_type in (1, 3):  # SystemRequired, ExecutionRequired
            if not k32.PowerSetRequest(handle, request_type):
                raise C.WinError(C.get_last_error())
            active.append(request_type)
        yield
    finally:
        for request_type in reversed(active):
            k32.PowerClearRequest(handle, request_type)
        k32.CloseHandle(handle)
```

**[A/D]** 验证时在任务期查看 `powercfg /requests`，再检查任务结束后请求消失；Microsoft 说明 WPA 的 Power Requests 视图可核查 Modern Standby 会话中 `System Required`/`Execution Required` 请求。`powercfg /requests` 的具体显示仍需在目标机实测。若目标现象是完全唤醒、仅息屏时 AX211 断线，此 API 对无线驱动行为没有官方修复承诺。[Microsoft Modern Standby 分析](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/using-windows-performance-analyzer-to-analyze-modern-standby-issues)、[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)

## 三、Q2：CONNECTIVITYINSTANDBY

### 1. GUID、值与版本

- **[A]** Microsoft Learn 将该旧设置归入 `SUB_NONE`（“No subgroup settings”），其子组 GUID 为 `fea3413e-7e05-4911-9a71-700331f1c294`，设置 GUID 为 `f15576e8-98b7-4186-b944-eafa664402d9`，且为隐藏设置。旧电源项的值：`0=Disabled`（待机断开网络）、`1=Enabled`（待机保持连接）、`2=Managed by Windows`。原文关键句：“Deprecated starting in Windows 10, version 2004.” Microsoft 的 Surface 特定说明仍使用 `CONNECTIVITYINSTANDBY` 这个 `powercfg` 别名处理 Surface Studio 2 的 WoL；这不等于 ASUS/Win11 上它可可靠强制保网。[Microsoft 子组定义](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/no-subgroup-settings)、[Microsoft 设置定义](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/no-subgroup-settings-allow-networking-during-standby)、[Microsoft Surface WoL 特例](https://learn.microsoft.com/en-us/surface/wake-on-lan-for-surface-devices)
- **[A]** 不要把“旧电源项弃用”误读为“同 GUID 的全部策略消失”：Microsoft 的 `ADMX_Power` 文档仍列出适用于 Windows 11 的 `ACConnectivityInStandby_2`、`DCConnectivityInStandby_2` 管理策略，同一 GUID 下分别用 `ACSettingIndex`/`DCSettingIndex`。启用策略称待机期间保持网络；禁用时文档只说连接**不保证**，且当前限制针对 WLAN。文档列出的 CSP 版本/版本系列包括 Win11；目标机具体 Windows 版本与是否有组织策略需另查。[Microsoft ADMX Power Policy CSP](https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-admx-power)

### 2. 本场景的参与时机

- **[A]** 该设置的定义始终是 “during standby”。合盖动作=0、系统完全唤醒且仅显示屏关闭时，尚未进入 standby，此设置不负责正常运行时的 Wi‑Fi 连接。若息屏后实际上进入了 S0 Modern Standby，应以电源报告核实，而不是仅凭黑屏推断。[Microsoft 设置定义](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/no-subgroup-settings-allow-networking-during-standby)、[Microsoft Modern Standby 概览](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby)
- **[A]** Windows 10 2004 起，Modern Standby 默认采用 Adaptive Connected Standby（ACS）：电池供电时 Windows 按需要决定睡眠期联网；文档列举远程桌面和需网络的 UWP 后台通知等场景。可用 `powercfg /spr` 的 **Networking in standby** 字段核查实际睡眠会话。公开 Microsoft 文档没有把 Wi‑Fi **专用/公用网络配置文件**列为 ACS 的唯一开关，也没有给出“ASUS OEM 必定覆盖该设置”的规则；不能用这两者直接判因。[Microsoft Modern Standby 网络连接](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-network-connectivity)
- **[A/D]** ARM 任务结束释放保护后，系统若按正常空闲策略进入 Modern Standby，桌面应用可能被 DAM 暂停，待机网络可能转入安静/受限模式，恢复时客户端重连**符合 Windows 设计允许的结果**。这是机制级解释，不证明目标机某一次 `Reconnecting` 必由休眠引起；要用睡眠会话时间、Wi‑Fi/NetworkProfile 事件与 app-server 的任务状态对齐。若任务确已结束，ARM 无须仅为避免该 UI 重连而无限延长保护。[Microsoft 应用集成](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/integrating-apps-with-modern-standby)、[Microsoft 待机网络电源管理](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/networking-power-management-for-modern-standby-platforms)、[Microsoft Modern Standby 网络连接](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-network-connectivity)

## 四、Q3：AX211 息屏掉线处置

### 1. 四项设置的证据与适用边界

| 候选处置 | 官方依据与证据等级 | 对“完全唤醒、仅息屏”掉线的结论 |
| --- | --- | --- |
| a. 取消设备管理器“允许计算机关闭此设备以节约电源” | **[A]** Intel 正式说明该复选框**不影响 Wi‑Fi 省电管理**，只影响 suspend/hibernate 时 Windows 与驱动对设备断电的处理，并建议保留默认勾选。[Intel 电源设置](https://www.intel.com/content/www/us/en/support/articles/000046272/wireless/legacy-intel-wireless-products.html) Intel 社区的个案曾建议尝试取消，但用户反馈无效；这是诊断建议/单例反馈，不是修复证据。[Intel AX211 论坛个案](https://community.intel.com/t5/Wireless/AX211-keeps-disconnect-after-boot-or-wake-from-sleep/td-p/1463597) | **[A]** 不应列为醒着息屏时的优先修复；它针对真正挂起/休眠更有相关性。 |
| b. 无线适配器“省电模式→最高性能” | **[A]** Intel 正式说明此模式让无线适配器优先性能，并提示电池续航代价。[Intel 电源设置](https://www.intel.com/content/www/us/en/support/articles/000046272/wireless/legacy-intel-wireless-products.html) GUID 为**子组** `19cbb8fa-5279-450e-9fac-8a3d5fedd0c1`、**设置** `12bbebe6-58d6-4636-95bb-3217ef867c1a`，`0=Maximum Performance`；提示词把子组误认作 `SUB_NONE`。这些 GUID/值在 Microsoft Learn 的用户 `powercfg /query` 实例可核对，但不是 Microsoft 正式电源项定义页，故 GUID 数值本身按 **[B]** 处理，并应在目标机 `powercfg /query` 再验。[Microsoft Q&A 的 `powercfg` 实例](https://learn.microsoft.com/en-us/answers/questions/2128350/confusion-between-hibernate-and-sleep) | **[A/D]** 有合理机制相关性，宜先记录 AC/DC 当前值，再做单变量合盖测试；**未找到** AX211 醒着息屏时必然有效的官方或可复现实验。 |
| c. 直装最新 Intel 通用驱动替代 ASUS OEM | **[A]** Intel 目前列出支持 AX211 的 Windows 驱动包 **24.70.0**、AX211 驱动 **24.70.0.3**；发行说明只笼统写更可靠连接，**没有**注明修复 AX211 息屏掉线。[Intel 下载页](https://www.intel.com/content/www/us/en/download/19351/intel-wireless-wi-fi-drivers-for-windows-10-and-windows-11.html)、[Intel 24.70.0 发行说明](https://downloadmirror.intel.com/926939/ReleaseNotes_WiFi_24.70.0.pdf) Intel 对 OEM 设备建议先用厂商驱动以获最佳兼容性，ASUS 也推荐 MyASUS/官网更新。[Intel OEM 驱动说明](https://www.intel.com/content/www/us/en/support/articles/000088527/wireless.html)、[ASUS 驱动更新](https://www.asus.com/support/faq/1051439/) | **[A/D]** 先比对当前版与 ASUS 对应机型最新版；仍复现时再以可回退的单变量试验测试 Intel 通用版。不能把“版本最新”当作该故障已修复。 |
| d. MIMO 省电模式 / 漫游积极性 | **[A]** Intel 说明 `No SMPS` 可绕开**某些旧 AP** 的 SMPS 兼容性/链路质量问题；漫游积极性控制何时扫描其他 AP，默认 Medium，若调整无改善建议恢复默认。[Intel 高级设置](https://www.intel.com/content/www/us/en/support/articles/000044644/wireless/legacy-intel-wireless-products.html)、[Intel 漫游说明](https://www.intel.com/content/www/us/en/support/articles/000005546/wireless/legacy-intel-wireless-products.html) | **[D]** 仅当日志显示 AP 兼容/漫游相关线索时再逐项试验；没有 AX211 息屏掉线的专门修复证据。 |

### 2. AX211 官方回应、版本和证据链

- **[A/D]** Intel 员工在一个 AX211 **唤醒后**掉线论坛帖中建议核对 OEM 驱动、复选框、AP、路由器固件及 BIOS；发帖人称切换复选框无效。该帖与“醒着、仅息屏”场景不同，不能移植为修复结论。就检索到的 Intel/ASUS 资料，没有找到针对“AX211 息屏即掉线”的官方确认、专用修复版本号或复现实验；**未找到公开来源**。[Intel AX211 论坛帖](https://community.intel.com/t5/Wireless/AX211-keeps-disconnect-after-boot-or-wake-from-sleep/td-p/1463597)、[Intel 24.70.0 发行说明](https://downloadmirror.intel.com/926939/ReleaseNotes_WiFi_24.70.0.pdf)
- **[A/D]** 对题设的严格状态（机器确实完全唤醒）而言，a 项被 Intel 说明直接削弱；b 项有官方机制说明，但缺少“息屏前后 AX211 保持连续连接”的因果证据。故目前没有“a/b 已被证实能阻止掉线”的证据链。建议实验记录：当前电源来源、`powercfg /query` 中该项 AC/DC 值、AX211 驱动版本、屏幕关闭时间、系统是否进入 S0、Wi‑Fi/NetworkProfile 事件与连续网络探测；一次只改一个值，并记录恢复值。实验设计属建议，尚无目标机实测。[Intel 电源设置](https://www.intel.com/content/www/us/en/support/articles/000046272/wireless/legacy-intel-wireless-products.html)、[Microsoft Modern Standby 网络连接](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-network-connectivity)

## 五、Q4：Codex 桌面端 Reconnecting

### 1. 已知报告与修复状态

- **[D：单一用户报告，托管于官方仓库]** `openai/codex` 的 Windows issue [#45099](https://github.com/openai/codex/issues/45099) 准确报告了 `Reconnecting... waiting for network`，记录版本 `26.908.40834`、`thread/start`/`turn/start` 成功、`model/list` 超时，以及 renderer 对未知 conversation 的日志。页面截至本报告日仍为 **Open**，未列关联 PR 或修复版本。它提示 UI 文字可能覆盖客户端状态同步失败，但**没有**官方根因确认；该报告中的任务最终也没有完成，故不能据此说“只是 UI 显示问题”。另有 Windows issue [#17610](https://github.com/openai/codex/issues/17610) 报告 `Reconnecting... 5/5` 与子进程退出超时，但症状与 #45099 不完全相同。
- **[D：单一用户报告]** [#33650](https://github.com/openai/codex/issues/33650) 报告经独立 app-server 发起的回合收到 `turn/completed`，桌面 UI 却未显示；这证明“UI 与任务状态可不一致”在某一桥接场景下有用户复现，并**不能**证明目标机此时的 `Reconnecting` 一定不影响任务。[OpenAI issue #33650](https://github.com/openai/codex/issues/33650)

### 2. 连接形态与怎样验证任务是否继续

- **[A]** OpenAI 的 app-server 架构文章明确说明：本地桌面 App 通常启动一个长寿命 app-server 子进程，通过 **JSON-RPC over stdio (JSONL)** 双向通信；线程事件持久化，`turn/started` 与 `turn/completed` 是不同生命周期节点。文章描述的 **HTTP+SSE** 属于 Codex Web 浏览器到后端的链路，不应直接套到 Windows 本地桌面端。公开源码另支持 app-server 的可选 WebSocket transport；但不能由此断言目标桌面版与本地 app-server 默认用 WebSocket，也不能确定桌面端到 OpenAI 服务端的每条内部连接协议。[OpenAI app-server 架构](https://openai.com/index/unlocking-the-codex-harness/)、[OpenAI transport 源码](https://github.com/openai/codex/blob/main/codex-rs/app-server-transport/src/transport/mod.rs)
- **[A/B]** 判定某次是否只是 UI 掉线，至少应核对同一 `threadId`/`turnId` 的 `turn/completed`（以及是否有错误）、可读的 `thread/read(includeTurns=true)` / 持久化会话记录，和实际产物或命令结果。仅 `thread/start`、`turn/start` 成功不够。OpenAI 官方文章定义回合以 `turn/completed` 结束；官方仓库测试源码展示收到 `turn/completed` 后读回线程。具体日志字段（例如 `model/list` 超时、`No promise for request ID`、`Received turn/started for unknown conversation`）来自 #45099 用户提交，适合作为排查线索，不是通用的官方诊断判据。[OpenAI app-server 架构](https://openai.com/index/unlocking-the-codex-harness/)、[OpenAI app-server 测试源码](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_shell_command.rs)、[OpenAI issue #45099](https://github.com/openai/codex/issues/45099)
- **[D]** 截至检索时，未找到针对该**具体 Windows 桌面提示**的 OpenAI 官方解释、确定修复版本、或对断线后本地任务继续运行的无条件保证；**未找到公开来源**。Remote Control 路径另有 [#31973](https://github.com/openai/codex/issues/31973) 用户报告 WebSocket/重连问题，但那是手机远控链路，不等于本地桌面 app-server 或模型服务链路。

## 六、对 ARM 的落地建议

**(a) [A/D]** **不把 `PowerSetRequest(ExecutionRequired)` 作为合盖断网的必选修复或替代现有机制。** 可在任务期以受控实验叠加 `ExecutionRequired` + `SystemRequired`，任务结束匹配清除并关闭句柄，确认 `powercfg /requests` 与实际合盖结果；但它只能覆盖调用它的 ARM 进程，不能保证 `ChatGPT.exe` 和网络。目标机现已在任务期关闭相关超时，若实测始终完全唤醒，新增请求的边际作用可能很小；这是基于题设的推断。[Microsoft `PowerSetRequest`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)、[Microsoft 待机网络电源管理](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/networking-power-management-for-modern-standby-platforms)

**(b) [A/B/D]** 合盖实测优先顺序：**先确认是“仅息屏”还是已进入 S0**；接着登记并试验无线适配器省电模式 AC/DC 值（Intel 对其作用为 **A**，GUID 需目标机复核为 **B**），再核对 ASUS OEM 驱动并在仍复现时单独测试 Intel 驱动（更新渠道为 **A**，对该故障有效性为 **D**）。只有 AP/漫游证据时才测试 `No SMPS` 或漫游值；“允许关闭设备”复选框不作为醒着息屏场景的优先项（Intel 明确说明为 **A**）。每次改动前后记录值并能恢复。[Intel 电源设置](https://www.intel.com/content/www/us/en/support/articles/000046272/wireless/legacy-intel-wireless-products.html)、[Intel OEM 驱动说明](https://www.intel.com/content/www/us/en/support/articles/000088527/wireless.html)、[Intel 高级设置](https://www.intel.com/content/www/us/en/support/articles/000044644/wireless/legacy-intel-wireless-products.html)

**(c) [A/D]** **是，若任务已结束并释放保护，随后按空闲策略进入 Modern Standby，桌面应用暂停及恢复后的重连属于设计允许的结果，ARM 无须为“完成后永不重连”延长保护。** 但某一次 UI 重连是否由睡眠引起仍为 **D**，须以睡眠报告和同一回合的 `turn/completed`/产物判定；任务未完成时的重连应继续排障。[Microsoft 应用集成](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/integrating-apps-with-modern-standby)、[Microsoft Modern Standby 网络连接](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-network-connectivity)、[OpenAI app-server 架构](https://openai.com/index/unlocking-the-codex-harness/)

## 七、来源链接汇总

**Microsoft**

1. [`PowerSetRequest` Win32 API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powersetrequest)
2. [`PowerCreateRequest` Win32 API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powercreaterequest)
3. [`PowerClearRequest` Win32 API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-powerclearrequest)
4. [`REASON_CONTEXT` 结构](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-reason_context)
5. [`POWER_REQUEST_TYPE` 枚举](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_power_request_type)
6. [Modern Standby 应用集成](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/integrating-apps-with-modern-standby)
7. [Modern Standby 网络电源管理](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/networking-power-management-for-modern-standby-platforms)
8. [Modern Standby 网络连接 / ACS](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-network-connectivity)
9. [Modern Standby 概览](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby)
10. [Modern Standby WPA 分析](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/using-windows-performance-analyzer-to-analyze-modern-standby-issues)
11. [`SUB_NONE` 子组](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/no-subgroup-settings)
12. [`CONNECTIVITYINSTANDBY` 旧设置](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/no-subgroup-settings-allow-networking-during-standby)
13. [Windows 11 ADMX Power Policy CSP](https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-admx-power)
14. [Microsoft Q&A 所附 `powercfg /query` 实例（用户内容，非正式规范）](https://learn.microsoft.com/en-us/answers/questions/2128350/confusion-between-hibernate-and-sleep)
15. [Surface Studio 2 WoL 中的 `CONNECTIVITYINSTANDBY` 特例](https://learn.microsoft.com/en-us/surface/wake-on-lan-for-surface-devices)

**Intel / ASUS**

16. [Intel 无线适配器电源管理说明](https://www.intel.com/content/www/us/en/support/articles/000046272/wireless/legacy-intel-wireless-products.html)
17. [Intel AX211 Windows 驱动下载页](https://www.intel.com/content/www/us/en/download/19351/intel-wireless-wi-fi-drivers-for-windows-10-and-windows-11.html)
18. [Intel 24.70.0 发行说明](https://downloadmirror.intel.com/926939/ReleaseNotes_WiFi_24.70.0.pdf)
19. [Intel OEM 设备驱动来源说明](https://www.intel.com/content/www/us/en/support/articles/000088527/wireless.html)
20. [Intel 无线适配器高级设置说明](https://www.intel.com/content/www/us/en/support/articles/000044644/wireless/legacy-intel-wireless-products.html)
21. [Intel 漫游积极性说明](https://www.intel.com/content/www/us/en/support/articles/000005546/wireless/legacy-intel-wireless-products.html)
22. [Intel AX211 社区个案（用户反馈与 Intel 员工回复）](https://community.intel.com/t5/Wireless/AX211-keeps-disconnect-after-boot-or-wake-from-sleep/td-p/1463597)
23. [ASUS BIOS/驱动更新说明](https://www.asus.com/support/faq/1051439/)

**OpenAI**

24. [OpenAI App Server 架构文章](https://openai.com/index/unlocking-the-codex-harness/)
25. [OpenAI app-server transport 源码](https://github.com/openai/codex/blob/main/codex-rs/app-server-transport/src/transport/mod.rs)
26. [OpenAI app-server 测试源码](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_shell_command.rs)
27. [Windows `Reconnecting... waiting for network` issue #45099](https://github.com/openai/codex/issues/45099)
28. [Windows `Reconnecting... 5/5` issue #17610](https://github.com/openai/codex/issues/17610)
29. [app-server 完成但桌面 UI 不显示 issue #33650](https://github.com/openai/codex/issues/33650)
30. [Windows Remote Control 重连 issue #31973](https://github.com/openai/codex/issues/31973)

## 八、事实 / 推测 / 未找到公开来源 项清单

| 类别 | 项目 |
| --- | --- |
| **事实 [A]** | `PowerSetRequest` 作用于调用进程；`SystemRequired` 管自动睡眠、`DisplayRequired` 管亮屏；S0/DC 有请求时限及主动睡眠终止规则；API 位于 `Kernel32.dll`。见第二节 Microsoft API 链接。 |
| **事实 [A]** | `SUB_NONE=fea3413e-7e05-4911-9a71-700331f1c294`，`CONNECTIVITYINSTANDBY=f15576e8-98b7-4186-b944-eafa664402d9`；旧电源项自 2004 弃用；同 GUID 的 AC/DC ADMX 策略仍在 Windows 11 文档列出。见第三节 Microsoft 链接。 |
| **事实 [A]** | Intel 称设备管理器该复选框不控制 Wi‑Fi 正常省电；最高性能影响适配器性能/耗电取舍；Intel 24.70.0 发行说明未列 AX211 息屏修复。见第四节 Intel 链接。 |
| **事实 [A/B]** | OpenAI 文档说明本地 app-server 为 stdio JSON-RPC，`turn/completed` 表示回合结束；官方仓库存在含确切提示的用户 issue，但其根因叙述仍是用户报告。见第五节 OpenAI 链接。 |
| **推测 [D]** | 目标机合盖后若系统确实未睡眠，`ExecutionRequired` 对网络问题边际作用小；任务完成后某次重连可能是进入 Modern Standby；OEM 可能有额外策略影响。均需目标机日志/合盖实测。 |
| **未找到公开来源** | AX211“仅息屏、系统完全唤醒”掉线的 Intel/ASUS 专项修复版本或 a/b 已验证有效的可复现实验；该 ASUS 机型的 OEM 对 `ExecutionRequired` 的具体削弱证据；OpenAI 对 #45099 的官方根因和修复版本；Windows 桌面版到云端所有连接的完整协议及该 UI 状态必然不影响本地任务的官方保证。 |
