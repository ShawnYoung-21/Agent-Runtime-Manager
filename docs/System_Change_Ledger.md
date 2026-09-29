# 系统变更台账（System Change Ledger）

> **这是干什么的**：ARM 项目在开发过程中，往 Windows 系统里留下了一些"系统级"的东西（定时任务、开机自启、hooks、注册表、全局命令）。这个文件就是这些东西的**总账本**——每一项都写清楚：是什么、为什么存在、怎么查证、怎么删除。
>
> **使用协议**：
> 1. 任何 agent（Claude 或其他工具）要往系统级添加/修改东西（计划任务、自启、hooks、注册表、环境变量、全局安装），**必须先在本文档登记，完工后更新状态**。不允许留"无账之物"。
> 2. 用户想了解系统现状 → 对 agent 说"**读一下台账**"。
> 3. 用户想清理 → 对 agent 说"**按台账对账**"，agent 逐项核对系统实况、汇报差异和可删项，**经用户确认后才执行删除**。
>
> 最后盘点：2026-09-28（逐项实际查证，非凭记忆）

---

## 一览表

| # | 名称 | 类型 | 作用 | 状态 |
|---|------|------|------|------|
| 1 | ARM-App | 计划任务 | 开机登录自动启动 ARM 应用 | ✅ 在役（Running） |
| 2 | ARM-Watchdog | 计划任务 | 每 5 分钟检查，ARM 应用死了自动拉起 | ✅ 在役（Ready，2026-09-28 注册） |
| 3 | Claude hooks ×4 | settings.json 注入 | 让 ARM 感知 Claude 会话开始/结束/干活/歇 | ✅ 在役 |
| 4 | arm 命令 | uv 全局工具 | 终端里能敲 `arm status` 等命令 | ✅ 在役 |
| 5 | ~/.arm 目录 | 数据目录 | ARM 的数据库、日志、电源快照 | ✅ 在役（垃圾已清，仅剩 6 个在役文件） |
| 6 | ARM 控制台.vbs/.lnk | 桌面启动器 | 双击启动不弹黑窗 | ✅ 在役 |
| 7 | 合盖动作=0 | 注册表（powercfg） | 合盖不睡眠——合盖保活的一部分 | ✅ 在役（**勿随手恢复**） |
| 8 | 禁用/恢复S0 两个 .bat | 桌面遗物 | 早期 S0 实验，已证实此机型无效 | ✔️ 已删除（2026-09-28） |
| 9 | ARM-Daemon | 计划任务 | 旧架构遗留 | ✔️ 已删除（2026-09-26） |

---

## 各项详情

### 1. ARM-App（计划任务）

- **是什么**：Windows 开机自启项。登录时自动运行 `pythonw.exe -m arm.cli app`（ARM 主程序，托盘+窗口+引擎）。
- **为什么**：ARM 是常驻保镖，不开自启每次开机就得手动开。
- **查证**：`Get-ScheduledTask -TaskName ARM-App`
- **删除**（管理员 PowerShell）：`Unregister-ScheduledTask -TaskName 'ARM-App' -Confirm:$false`
- **删了会怎样**：开机不再自启，需手动启动 ARM。

### 2. ARM-Watchdog（计划任务）

- **是什么**：看门狗。登录时启动 + 每 5 分钟重复：检查 ARM 应用进程消失且心跳过期（>90 秒）→ 自动执行 `schtasks /Run ARM-App` 拉起来。用户主动退出 ARM 时会写退出标记，看门狗见了不打扰。
- **为什么**：ARM 自己的"进程内互护"在进程整个崩掉时会全灭，需要 OS 级的外部救援。
- **查证**：`Get-ScheduledTask -TaskName ARM-Watchdog`
- **删除**（管理员 PowerShell）：`Unregister-ScheduledTask -TaskName 'ARM-Watchdog' -Confirm:$false`
- **删了会怎样**：ARM 崩溃后不会自动复活（自愈能力降级，其余功能不受影响）。
- **注册命令**（万一要重建，管理员 PowerShell）：
  ```powershell
  $action = New-ScheduledTaskAction -Execute "%USERPROFILE%\AppData\Roaming\uv\tools\agent-runtime-manager\Scripts\pythonw.exe" -Argument "-m arm.cli watchdog"
  $t1 = New-ScheduledTaskTrigger -AtLogOn
  $t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
  Register-ScheduledTask -TaskName "ARM-Watchdog" -Action $action -Trigger $t1,$t2 -Description "ARM 全死复活看门狗"
  ```
  ⚠️ 坑：`-RepetitionDuration ([TimeSpan]::MaxValue)` 会被序列化成超范围 XML 被拒（截图里那个红错）；**省略该参数即无限重复**。

### 3. Claude hooks ×4（~/.claude/settings.json）

- **是什么**：在 Claude Code 的全局 settings.json 里注入了 4 个钩子（SessionStart / UserPromptSubmit / Stop / SessionEnd），每次 Claude 会话有事发生就调 `arm.exe hook-ingress` 报告给 ARM。
- **为什么**：ARM 靠这个感知"Agent 在不在干活"，决定要不要保活。
- **查证**：settings.json 里 `hooks` 段是否指向 `arm.exe hook-ingress`；备份在 `~/.claude/settings.arm-backup.json`
- **删除**：`arm init --undo`（自动还原备份并写抑制标记，哨兵不会再注入回来）
- **注意**：第三方工具（如 cc-switch）重写 settings.json 可能冲掉 hooks——ARM 的哨兵每 60 秒会自动修复（这是设计行为，不是异常）。

### 4. arm 命令（uv 全局工具）

- **是什么**：`uv tool install` 装的全局命令行工具，装在 `%APPDATA%\uv\tools\agent-runtime-manager\`，命令入口 `~/.local/bin/arm.exe`。
- **为什么**：终端里能直接敲 `arm status / arm ui / arm doctor` 等。
- **查证**：`uv tool list`
- **删除**：`uv tool uninstall agent-runtime-manager`（入口 exe 一并删）
- **坑**：重装前必须先停掉所有 ARM 进程（daemon 锁会锁死 arm.exe 导致装一半残废）。
- **操作记录**：2026-09-28 UI 按钮修复（1e42e4f）后重装：停 pythonw → `uv tool install --force` → `schtasks /Run ARM-App` 重启。注：任务管理器常看到两个 pythonw（一大一小）**不是残留**——小的是 uv venv trampoline 空壳，大的是实际解释器进程，父子关系属 uv tool 正常进程树。
- **操作记录**：2026-09-28 移除概览页冗余保护按钮（365bda6）后再次重装重启，流程同上。

### 5. ~/.arm 数据目录

- **是什么**：ARM 的全部本地数据：arm.db（SQLite 状态库）、arm.log（日志）、power_backup.json / power_button_backup.json（电源快照，**删了会导致电源设置无法还原**）、app_heartbeat（心跳）、各类标记文件。
- **删除时机**：只在彻底卸载 ARM 时删整个目录。平时**不要动 power_backup.json**。
- **历史垃圾**（arm.db.bak×2、d.err/d.out、armw_*.txt、pyw_trace.txt、ui.log、lid_test_baseline.txt、engine.lock）：✔️ 已清理（2026-09-28，用户确认）。
- **瞬态文件**（存在有义，勿手删）：app_quit_flag（优雅退出标记，看门狗据此不拉起）、hooks_suppress（init --undo 抑制标记，哨兵据此不重注入）、power_button_backup.json / power_backup.json（电源快照，仅在役时存在）。

### 6. 桌面启动器（ARM 控制台.vbs + .lnk）

- **是什么**：双击快捷方式 → wscript 静默运行 VBS → 拉起 ARM，不弹黑色控制台窗口。**一对搭档，缺一不可**（.lnk=入口，.vbs=它指向的脚本）。
- **当前状态**：.vbs 已设隐藏属性（2026-09-28，用户确认）——桌面只显示快捷方式一个图标，功能不变。
- **删除**：直接删桌面这两个文件（.vbs 隐藏后需开"显示隐藏文件"或按路径删）。
- **删了会怎样**：双击启动没了，但计划任务自启和 `arm app` 命令仍可用。

### 7. 合盖动作=0（注册表，经 powercfg 写入）

- **是什么**：电源计划里"合上盖子"的动作被设为"不采取任何操作"（AC/DC 均 0）。这是合盖保活方案的组成部分。
- **⚠️ 注意**：这是**在役功能**，不是垃圾。除非彻底卸载 ARM，否则不要"顺手恢复"。
- **恢复方法**（卸载 ARM 时才用）：
  ```powershell
  powercfg /setacvalueindex scheme_current sub_buttons lidaction 1
  powercfg /setdcvalueindex scheme_current sub_buttons lidaction 1
  powercfg /setactive scheme_current
  ```

### 8. 桌面两个 .bat（S0 实验遗物）✔️ 已删除

- **是什么**：`禁用S0待机_需管理员.bat` / `恢复S0待机_需管理员.bat`。早期尝试用注册表禁用 Modern Standby，已实测**此机型（ASUS Vivobook S15 + Win11 26200）不认**，无效。
- **状态**：✔️ 已删除（2026-09-28，用户确认）。

### 9. ARM-Daemon（已删除）✔️

- 旧双进程架构的开机自启项，2026-09-26 架构合并（daemon 并入 app 单进程）时已由用户在管理员终端删除。2026-09-28 核实系统里已不存在。`arm daemon` 命令还在但仅作备用入口。

---

### 10. 电源键动作运行期钉住（瞬态，2026-09-28 引入）

- **是什么**：ARM 应用运行期间，电源计划里"电源键动作"被临时设为"不采取任何操作"（防误按睡眠冻结 agent）；退出 ARM 自动还原为原值（本机原值=睡眠）。
- **为什么**：二态模型——ARM 在=全职保镖（按电源键不睡），退出=一切休息。
- **查证**：`powercfg /q scheme_current sub_buttons pbutton`（ARM 运行时应为 0，退出后为 1）；快照在 `~/.arm/power_button_backup.json`
- **还原**：退出 ARM 自动还原；崩溃遗留由下次启动自动"先还原再重钉"。
- **边界**：长按 4 秒强关是硬件级行为，不受影响；开始菜单"睡眠/关机"不受影响。

### 11. 无线适配器省电模式 DC=最高性能（单变量实验，2026-09-29 登记）

- **是什么**：把电源计划"无线适配器设置→省电模式"的**使用电池**值从 2（中等省电）改为 0（最高性能）。AC 保持 0 不动。
- **为什么**：2026-09-28 用户实测：**用电池+ARM 保护中（系统醒着）+合盖**，Codex 桌面端报 `Reconnecting... waiting for network`。该场景下睡眠/合盖/AC 全部排除，唯一处于"省电"状态的电源杠杆就是 DC 无线省电=2（GUID 子组 `19cbb8fa-5279-450e-9fac-8a3d5fedd0c1`、设置 `12bbebe6-58d6-4636-95bb-3217ef867c1a`，本机已验证存在）。外部调研定级：该设置有 Intel 官方机制说明（省电模式牺牲性能换续航），AX211 息屏掉线为社区已知问题。
- **查证**：`powercfg /q scheme_current 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a`（改动后 AC=0 / DC=0）
- **还原**（实验失败或要回退时）：
  ```powershell
  powercfg /setdcvalueindex scheme_current 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 2
  powercfg /setactive scheme_current
  ```
- **验收**：改后重做"任务+拔电+合盖"实测——不再出现 Reconnecting 即嫌疑坐实；若坐实，讨论是否产品化（ARM 保护期自动钉 DC 无线=0、释放还原，与睡眠三项同模式）。
- **副作用**：电池续航略降（网卡不做激进入睡省电）；系统睡眠行为不受影响（本机睡眠超时本就是永不）。
- **执行记录**：⬜ 待执行（用户确认后）

### 12. 新版 ARM 重装（2026-09-29，会话级判定+网络分层诊断版）

- **是什么**：停所有 arm 进程 → `uv tool install --force .`（从 worktree 安装含以下新能力的版本）→ 经 `schtasks /Run ARM-App` 重启。
- **新能力**：①Codex 会话级忙闲判定（timeline 终态优先，修复"任务结束仍显示在线"）；②网络分层诊断（网关/DNS/公网 TCP）+ UI 顶栏网络告警；③合盖测试报告新增 SleepStudy HTML 证据与三层网络证据；④ChatGPT.exe/codex app-server 常驻不再作为任务兜底。
- **为什么**：旧版把全局 WAL 活动误归因给最新会话导致保护不释放、活动显示错误；且合盖断网无可观测证据。实测（用电池+保护中+合盖）确认掉网后，再执行第 11 项实验。
- **还原**：git 分支 `claude/quirky-moore-4de65d`（commits f1237a5..ad9b8f4），需要时可从 main 重新安装旧版。
- **执行记录**：✅ 已执行（2026-09-29：无残留进程 → `uv tool install --force .` → `schtasks /Run ARM-App` 成功；心跳 1s 内恢复，引擎进入 PROTECTING 保护当前活跃会话）

---

## 外部清理记录（非 ARM 项，2026-09-28）

用户要求全盘审计"任务计划程序"后清理。21 个非 Microsoft 任务审计：12 个在役保留（ARM×2、Aily CLI、Chrome×3、OneDrive×3、WPS×3、系统 Feed 同步），其余处置如下：

- ✔️ **已删（普通权限即删成）**：`BlueStacksHelper_nxt`（蓝叠模拟器已卸载，exe 已不存在）、`QuarkUpdaterTaskUser1.0.0.21{...}`（夸克更新器 exe 已不存在）、`SoftLanding\...` ×3（微软推广/广告任务；注：系统大更新可能带回来，对账时复查）
- ✔️ **已删**：`runFeikuaFirewallSetup`、`startFeikuaMemreduct`、`startFeikuaUpdate`（"飞快"软件已卸载的僵尸任务；管理员权限安装的任务普通权限删不掉，用户已在管理员终端执行，2026-09-28 核实不存在）。至此本节 8 项清理全部完成，非系统任务 21 → 13。

- **经验**：`Unregister-ScheduledTask` 普通权限即可删"当前用户级"任务（SoftLanding/Quark/BlueStacks 实测可删）；管理员/提权安装的软件注册的任务才需要管理员终端。

---

## 对账命令块（任何 agent 拿去直接跑）

```powershell
# 计划任务
Get-ScheduledTask -TaskName 'ARM-App','ARM-Watchdog' -ErrorAction SilentlyContinue | Select TaskName,State
# hooks 是否在位（True=在）
Select-String -Path "$env:USERPROFILE\.claude\settings.json" -Pattern 'arm\.exe' -Quiet
# 备份文件是否存在
Test-Path "$env:USERPROFILE\.claude\settings.arm-backup.json"
# 全局命令
uv tool list
# 数据目录
ls "$env:USERPROFILE\.arm"
```

## 彻底卸载 ARM 的完整顺序（需要时再做）

1. 管理员 PowerShell：`Unregister-ScheduledTask -TaskName 'ARM-App' -Confirm:$false`（ARM-Watchdog 同理）
2. `arm init --undo`（还原 hooks）
3. `uv tool uninstall agent-runtime-manager`
4. 删桌面：`ARM 控制台.vbs`、`ARM 控制台.lnk`、两个 S0 .bat
5. 恢复合盖动作（见第 7 项命令）
6. 删 `~/.arm` 整个目录
