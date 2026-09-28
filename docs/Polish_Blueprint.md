> [历史文档] 本文档描述单进程合并（2026-09-26）之前的架构与计划，**现状以 [docs/Self_Healing_Manual.md](Self_Healing_Manual.md) 为准**。

# Agent Runtime Manager — 打磨蓝图 v1.0（2026-09-26）

> 输入：首次真实合盖验收（成功）暴露的问题 + 全面代码审查
> 原则：多层次兜底、可观测、可自愈、UX 诚实

## 里程碑（已达成）
✅ 核心场景验证成功：合盖+电池，daemon 保活，任务 2.5 分钟跑完（23:05-23:07 实证）
✅ 双信号感知（hooks 精准 + 进程兜底）、跨进程同步、崩溃自愈、僵尸回收

## L1 感知层 — 三信号体系
| # | 问题 | 方案 | 优先级 |
|---|---|---|---|
| 1.1 | **Stop 事件在 S0 下丢失**（23:07:42 任务完成，hook 未落库） | **T14 transcript 旁路**：daemon 周期扫 `~/.claude/projects/*/*.jsonl` 的 mtime+末条时间戳，作为"会话活跃"旁路信号；Stop 丢失时可自愈（transcript 显示完成→更新状态） | 🔴 P0 |
| 1.2 | hook 失败无感知 | hook-ingress 失败时写本地文件日志（`~/.arm/hook_errors.log`），daemon 定期检查上报 | 🟡 P1 |
| 1.3 | 网络感知还没接（Research 问题四） | T8：ping 网关+LLM 代理端口，断网时 UI 横幅提示"任务可能停滞"（保活不撤） | 🟡 P1 |

## L2 决策层 — 状态机完善
| # | 问题 | 方案 | 优先级 |
|---|---|---|---|
| 2.1 | 任务完成但进程还在（会话窗口没关）→ 永远 PROTECTING | transcript 旁路发现"末条 assistant 消息 + 超过 finish_grace"→ 判 FINISHED | 🔴 P0（与 1.1 同体） |
| 2.2 | 多会话并存时，保护决策无"哪个会话在跑"的粒度 | status/UI 显示每个进程对应的最新会话（按 cwd 匹配近似） | 🟢 P2 |
| 2.3 | 无配置文件（finish_grace/超时硬编码） | config.toml 支持 `finish_grace_s/session_timeout_s/poll_interval_s` | 🟢 P2 |

## L3 执行层 — 电源控制
| # | 问题 | 方案 | 优先级 |
|---|---|---|---|
| 3.1 | ✅ 已验证 ES_AWAYMODE 有效 | — | — |
| 3.2 | 防节流未接真实 PID（_active_pids 空） | 保护期对探测到的 Claude PID 调 disable_throttling | 🟡 P1 |
| 3.3 | 电池策略一刀切 | 电量 < 20% 且电池模式：UI 强提示（是否暂停保护由用户决定，不自动撤——可靠性优先） | 🟢 P2 |

## L4 可观测层 — 让一切可追溯
| # | 问题 | 方案 | 优先级 |
|---|---|---|---|
| 4.1 | daemon 无日志文件（出问题没法回溯） | `~/.arm/arm.log`：tick 节拍日志（状态变迁/错误），轮转保留 7 天 | 🔴 P0 |
| 4.2 | 时间线缺少"会话与任务的关联" | transcript 旁路顺带记录会话的项目路径 → 时间线显示"哪个项目在跑" | 🟡 P1 |
| 4.3 | 心跳只有"几秒前"，无历史 | 心跳历史表（每分钟采样），UI 小图看"合盖期间心跳是否连续" | 🟢 P2 |

## L5 UI/UX 层 — 诚实、讲故事、可操作
| # | 问题 | 方案 | 优先级 |
|---|---|---|---|
| 5.1 | **测试成功/失败 UI 上看不出来**（你问"怎么看出来成功没"） | **测试报告卡**：一键"生成合盖测试报告"——对比基线快照与当前（事件/心跳/电源），自动判定 ✅/❌ 并展示证据链 | 🔴 P0 |
| 5.2 | 时间线不显示项目名（session id 无意义） | 时间线+会话表显示项目目录名（cwd 尾段），如"Agent Runtime Manager" | 🟡 P1 |
| 5.3 | 会话表信息密集无重点 | 活跃会话卡放大置顶：cwd+运行时长+状态大字 | 🟡 P1 |
| 5.4 | 空/加载态粗糙 | 首次使用引导（hooks 没注入时给一键 init 按钮） | 🟢 P2 |
| 5.5 | 无暗/亮主题切换 | 跟随系统（prefers-color-scheme） | 🟢 P2 |

## 实施结果（2026-09-26 完成）
✅ T14 transcript 旁路（1.1+2.1+4.2）— sensors/transcript.py + _reconcile_with_transcripts，91 tests
✅ arm.log（4.1）— core/logging_util.py 轮转日志
✅ 合盖测试报告卡（5.1）— /api/lid-baseline + /api/lid-report + UI 两按钮
✅ 防节流接 PID（3.2）— _active_pids 返回真实 Claude PID
✅ 项目名显示（5.2）— proj_by_sid 进时间线和会话表

## 追加完成（2026-09-26 凌晨，自愈三件套）
✅ hooks 自动哨兵（1.2 强化）— _hooks_sentinel：60s 巡检、有 init 证据自动重注入、HOOKS_REPAIRED 进时间线
✅ daemon 开机自启（根治"忘开 daemon 裸奔"）— 计划任务 ARM-Daemon（AtLogOn）+ arm install-daemon 命令
✅ 真机验证：删 hooks → daemon 5 秒自愈；计划任务触发 → daemon 拉起心跳正常

## T15 电源组合拳（2026-09-26 完成，竞品研究成果）
✅ control/power_policy.py：powrprof API 直读直写四项（UNATTENDSLEEP/STANDBYIDLE/HIBERNATEIDLE/VIDEOIDLE），快照落盘+恢复+滞留自愈
✅ 引擎接入：进保护钉住、释放还原、启动自愈滞留快照（比竞品的内存备份更安全）
✅ 真机全链路验证：protect→全 0、release→原值精确还原。100 tests。
✅ 竞品研究结论：Always-Up 验证赛道；差异化=生命周期驱动 vs 手动开关。GUID 须结构体 byref 传。

## 剩余（下轮）
- hook 错误日志（1.2）
- 网络感知 T8（1.3）
- 多会话粒度（2.2）、config.toml（2.3）、低电策略（3.3）
- 心跳历史图（4.3）
- 活跃会话大卡（5.3）、首次引导（5.4）、主题切换（5.5）

## 明确不做（本轮）
- 心跳历史图（P2，下轮）
- 配置文件（P2，下轮）
- 主题切换（P2，下轮）
