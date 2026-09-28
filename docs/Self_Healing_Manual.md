# ARM 自愈机制手册（迭代用）

> 目的：系统任何一个环节出问题时，**把这份文档拉给 Claude 就能定位迭代**，不用重新布线。
> 更新：2026-09-28，随代码同步维护。

## 一、自愈机制全景（谁挂了谁来救）

| 故障 | 检测者 | 恢复动作 | 检测阈值 | 代码位置 |
|---|---|---|---|---|
| **app 进程全死**（崩溃/被杀；引擎同死，进程内互护全灭） | **ARM-Watchdog 计划任务**（进程外，每 5 分钟） | `arm watchdog` → `schtasks /Run ARM-App` | 心跳过期 >90s **且** 无 arm app 进程 **且** 无退出标记（托盘退出留标记=尊重用户，下次启动自动清除）；没装任务则不打扰 | cli.py `watchdog` |
| app 卡死但进程在（心跳停） | —（已移除 `_app_watchdog`：合并架构下它永远没机会真正拉起，OS 级看门狗已接管） | 引擎本身还在跑，保护不受影响；只有 UI 卡。重启 app 即恢复界面 | — | — |
| 托盘"退出"（优雅退出） | `NativeApp._quit` | 通知引擎 stop → `run()` finally 释放保护+还原电源。引擎线程**必须非 daemon**（daemon 线程被进程退出掐死、finally 不跑 → 电源滞留，2026-09-28 教训） | 即时 | app.py |
| **tick 级异常**（DB 锁抖动等） | run() 循环容错 | 记日志带病运行；连续 5 次失败退避 30s。单次异常不再终结引擎（旧版会瞎 90s） | 每拍 | runtime.py |
| 嵌入引擎线程死（app 壳还在） | app `_ensure_daemon` | Popen `arm daemon`（headless 接管引擎锁） | daemon 心跳无 + 30s + 60s 防抖 | app.py |
| hooks 被第三方工具冲掉 | 引擎 `_hooks_sentinel` | 自动重注入（备份→install） | 60s 巡检；判据=init 过（备份文件存在或曾见 OK）；**`init --undo` 留抑制标记（hooks_suppress）则永不自动修** | runtime.py |
| daemon 硬杀留下电源快照未还原 | 引擎启动时 | 读 ~/.arm/power_backup.json 自动还原 | 启动时一次 | runtime.py run() + power_policy.recover_if_stranded |
| **电源键误按**（app 运行期单击=睡眠会冻结 agent） | app 启动 `pin_power_button` | 快照原值→临时钉"不动作"；退出还原；崩溃遗留由下次启动"先还原再重钉"自愈 | app 启动/退出 | power_policy + app.py |
| arm.db 无界增长 | 引擎 `_maybe_prune` | 事件/环境事件留 30 天、终态会话留 14 天，RUNNING 永不动 | 每 6h | store.prune |
| **电源读写并发**（引擎/CLI protect 双写入方） | `_power_lock` 命名互斥体 | apply/restore 全程序列化（超时 5s 退化放行，宁钉勿滞） | 每次读写 | power_policy.py |
| hook 事件丢失 / **桌面会话不发提示词 hooks**（实测 Desktop 引擎只有 SessionStart，无 UserPromptSubmit/Stop） | `_reconcile_with_transcripts` | transcript 热（静默<30min）→ 摸新 updated_ts 防**误收僵尸**；静默超时 → 判 FINISHED/STOPPED 并释放保护 | idle 120s / busy 30min | runtime.py（**先 reconcile 再 reap**，顺序别反） |
| **长会话漏保护**（hooks 不发也能保） | `_any_agent_active` 第②信号 | transcript 忙（静默<90s）即算活跃，宁可误保护 | 每拍 | runtime.py |
| 僵尸会话（崩溃未发 SessionEnd） | `_reap_stale_sessions` | 回收 STOPPED（transcript 救不回才收） | busy 30min 无事件且 transcript 也静默 | runtime.py |
| 保护状态卡死（硬杀后假 PROTECTING） | 读取侧 | 心跳过期自动判 DISARMED（stale 显示） | 30s 无心跳 | store.py `get_effective_protection` |
| 跨进程状态分叉（UI/CLI vs 引擎） | 引擎 `_sync_with_store` | 库是唯一事实源，采纳外部 arm/release | 每拍 | runtime.py |
| **多引擎双持锁**（2026-09-28 事故根因） | 单实例锁 `use_last_error=True` | windll 直调 GetLastError 会被 ctypes 冲掉错误码→双引擎互搏→电源快照被"钉到一半"的值覆盖。已根治，勿改回 | 每次加锁 | core/single_instance.py |

## 二、进程模型（单进程 + OS 级看门狗）

```
ARM-App      计划任务(AtLogOn)  → pythonw -m arm.cli app
                                  └─ 单进程：引擎线程(2s/拍) + pywebview 窗口 + 托盘 + 内嵌UI(8620)
ARM-Watchdog 计划任务(每5分钟)  → pythonw -m arm.cli watchdog
                                  └─ 进程外保活：app 全死时 schtasks /Run ARM-App（见文末注册命令）
app 硬死 = 引擎同死，进程内互护（_app_watchdog/_ensure_daemon）全灭——只有任务计划程序能救。
进程间通过 ~/.arm/arm.db (SQLite WAL) 共享状态；电源读写经 Local\arm-power-mutex 序列化。
```

## 三、关键文件

| 文件 | 内容 |
|---|---|
| `~/.arm/arm.db` | 全部状态（agent_state/protection/env_events/daemon_heartbeat） |
| `~/.arm/arm.log` | 引擎日志（轮转 5MB×3），排障第一入口 |
| `~/.arm/power_backup.json` | 电源策略快照（存在=钉住未恢复；引擎启动会自愈；读写经互斥体，原值不会被并发覆盖） |
| `~/.arm/app_heartbeat` | app 心跳（看门狗依据） |
| `~/.claude/settings.json` | Claude hooks（arm init 注入 / 哨兵自动修复） |

## 四、排障决策树（出问题时按这个走）

```
保护没生效？
├─ arm status 看守护心跳
│   ├─ 心跳超过 35s → app/引擎死了 → 应被 ARM-Watchdog 拉起（事件流 APP_REVIVED，≤5 分钟）
│   │                  没装 ARM-Watchdog 任务？→ 文末注册命令（管理员）
│   └─ 心跳正常 → 看 arm.log 最后的 protection 行
│       ├─ "DISARMED (user release)" → 重新点开启保护
│       └─ "PROTECTING" 但担心没效果 → 合盖实测（基线→合盖→报告）
├─ 长会话中途保护自己撤了？
│   └─ 2026-09-28 已修（transcript 激活+摸心跳）。若复发：查 busy_sessions()
│       是否把该会话判忙（transcript 静默须 <90s）；桌面会话没有提示词 hooks 是已知边界
├─ 电源值被钉住没还原？
│   ├─ 引擎活着 → 释放保护即还原（release() 现在必还原）
│   └─ 引擎死了 → 重启 arm（启动自愈 recover_if_stranded）；
│       快照内容损坏（原值像 0）→ 从 CLI 会话 transcript 考古原值（2026-09-28 先例）
└─ hooks 被冲？
    └─ 引擎 60s 内自动修（事件流 HOOKS_REPAIRED）；治本=把 hooks 写进 cc-switch 模板
```

## 五、已知边界（诚实清单）

1. **桌面会话不发 UserPromptSubmit/Stop hooks**（Desktop 内嵌引擎行为，无法从外部修）→
   transcript 忙闲已兜底激活保护；会话行可能在纯桌面会话里显示 FINISHED（外观问题，不影响保护）
2. S0 下 hook 事件会丢（Windows 冻结 hook 子进程）→ transcript 旁路兜底
3. transcript 静默 90s 阈值：超长思考（>90s 无新行）会短暂误判空闲——进程兜底仍在，保护不会真撤
4. 任务完成判定是"静默"启发式，不理解语义（PRD 明确不做任务理解）
5. 电源快照恢复依赖 arm 能再启动（ARM-Watchdog 任务是进程外兜底）；卸载 arm 时如有滞留快照需手动还原
6. **电源键（二态模型，2026-09-28 定调）**：ARM 运行期间电源键单击临时钉"不动作"
   （防误按睡眠冻结 agent；快照在 ~/.arm/power_button_backup.json）；退出 ARM 自动还原=
   一切休息。长按 4 秒强关是硬件级、不受影响；开始菜单"睡眠/关机"不受影响。
   崩溃遗留快照由下次 app 启动"先还原再重钉"自愈
7. 关屏不参与保活（2026-09-28 复审）：away mode + 睡眠三项（无人值守/睡眠/休眠超时）钉"永不"即够，
   屏幕按用户自己的设置正常熄灭——省电且零风险
8. 启动布防尊重用户：上次是 "user release" → 本次启动保持未布防，手动开启即可
9. 测试纪律：引擎测试必须 mock 电源策略与 transcript 扫描
   （否则 pytest 会真钉真机电源、真拉起 app——2026-09-28 基线跑分实测发生过）

## 六、ARM-Watchdog 注册（管理员 PowerShell，一次性）

```powershell
$action = New-ScheduledTaskAction -Execute "C:\Users\yangchaoxin\AppData\Roaming\uv\tools\agent-runtime-manager\Scripts\pythonw.exe" -Argument "-m arm.cli watchdog"
$t1 = New-ScheduledTaskTrigger -AtLogOn
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "ARM-Watchdog" -Action $action -Trigger $t1,$t2 -Description "ARM 全死复活看门狗（app 进程消失且心跳过期时拉起）"
```

注意：`-RepetitionDuration` **不要传**（`[TimeSpan]::MaxValue` 会序列化成超范围 XML 值被拒，
实测 2026-09-28）；省略即"无限期重复"。

验证：`Start-ScheduledTask ARM-Watchdog` 后看 `~/.arm/arm.log`（app 活着时应无任何输出，静默退出）。
