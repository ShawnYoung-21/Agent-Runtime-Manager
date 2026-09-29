# ARM Modern Standby + Codex Desktop 合盖断连调研报告 v1.0

## 结论摘要

ARM 的核心设计方向正确：不能将 UI 状态、网络状态、进程状态直接等价为
Agent 任务状态。

关键结论：

-   SetThreadExecutionState 可以降低系统睡眠风险，但不能保证
    Wi-Fi、TCP、WebSocket、Electron 网络服务永久保持。
-   Modern Standby
    下普通桌面应用无法获得所有硬件上的合盖电池不断网保证。
-   ARM 应定位为 Agent Runtime Assurance Layer：
    -   任务保护
    -   状态判断
    -   断连诊断
    -   自动恢复
    -   失败解释

## 状态分层

Power State → Network State → Transport State → Application State →
Agent Session State → Task Result State

禁止判断：

-   ChatGPT.exe 存在 = 任务运行
-   全局 WAL 刷新 = 会话运行
-   UI reconnect = 任务失败
-   网络断开 = 任务结束

## ARM 当前方案审查

保留：

-   Session 级任务判断
-   completed/session-ended/interrupted 优先
-   网络与任务状态分离
-   SleepStudy 和事件日志

调整：

-   Away Mode 不作为默认模式
-   Sleep timeout 修改作为运行时保护策略

## 推荐架构

任务层：

TASK_RUNNING TASK_COMPLETED TASK_INTERRUPTED TASK_UNKNOWN

网络层：

CONNECTED LIMITED OFFLINE

传输层：

CONNECTED RECONNECTING FAILED

应用层：

APP_SERVER_RUNNING APP_SERVER_FAILED

## 下一步

1.  完成真实合盖实验采集。
2.  收集 SleepStudy、事件日志、网络状态。
3.  对比 ARM 开启和关闭。
4.  增加网络分层检测与 Codex session health。

## 边界

无法保证所有 Windows 11 S0
设备合盖电池不断网，需要依靠诊断和恢复机制提升可靠性。
