# ARM Modern Standby + Codex Desktop 合盖断连调研报告 v3.0 FINAL

## 一、最终结论

问题核心不是单一"断网"，而是 Windows Modern Standby
场景下多个层级状态可能不同步。

结论：

-   Modern Standby（S0 Low Power Idle）不是传统 S3 睡眠。
-   系统可能保持低功耗活动，但不能推导桌面应用长连接永久保持。
-   SetThreadExecutionState 可以帮助阻止部分睡眠路径，但不能保证
    Wi-Fi、TCP、WebSocket 或应用层连接。
-   ARM 应定位为 Agent Runtime Assurance Layer，而不是防睡眠工具。

参考： Microsoft Modern Standby 文档说明 S0 Low Power Idle
设计目标是低功耗并保持连接能力，但需要硬件、固件、驱动和软件共同支持。

## 二、核心状态分层

    Power Layer
        |
    Network Layer
        |
    Transport Layer
        |
    Application Layer
        |
    Agent Session Layer
        |
    Task Result Layer

禁止：

-   ChatGPT.exe 存在 = 任务运行
-   WAL 刷新 = 会话运行
-   UI reconnect = 任务失败
-   网络断开 = 任务结束

## 三、Power Layer

推荐：

保留：

    ES_CONTINUOUS | ES_SYSTEM_REQUIRED

用途：

-   降低系统进入睡眠风险。

不能保证：

-   WiFi不断；
-   TCP不断；
-   WebSocket不断；
-   Electron恢复。

Away Mode：

不作为默认能力。

## 四、Network Layer

必须分层：

    Adapter
     |
    WiFi Association
     |
    IP
     |
    Gateway
     |
    DNS
     |
    TCP 443
     |
    TLS
     |
    Application API

## 五、诊断实验矩阵

### 基线

开盖+电池。

采集：

    powercfg /a
    Get-NetAdapter
    Get-NetConnectionProfile
    netsh wlan show interfaces

### 核心实验

合盖+电池。

对比：

-   ARM开启
-   ARM关闭

增加：

-   合盖插电
-   USB网络设备
-   VPN/代理

## 六、SleepStudy

SleepStudy 用于分析 Modern Standby session。

用途：

故障后的证据分析：

-   是否进入 Modern Standby；
-   active 时间；
-   idle 时间；
-   活跃来源。

命令：

    powercfg /sleepstudy

## 七、ARM最终架构

    ARM Runtime Assurance Layer

    Power Sensor

    Network Sensor

    Transport Sensor

    Agent Sensor

    Recovery Engine

    Evidence Store

## 八、最终实施优先级

P0：

1.  Session级任务判断。
2.  网络分层检测。
3.  SleepStudy采集。
4.  合盖真实实验。

P1：

1.  Codex health check。
2.  自动恢复策略。
3.  Event Log/ETW分析。

P2：

1.  OEM驱动专项。
2.  NDIS深度分析。

## 九、事实 / 推测 / 未知

事实：

-   Modern Standby 为 S0 Low Power Idle。
-   SleepStudy 可分析 Modern Standby session。
-   S0 Connected / Disconnected 存在。

推测：

-   UI reconnect 可能不代表 Agent 停止。
-   Electron/Transport恢复可能影响显示。

未知：

-   ASUS具体硬件驱动行为。
-   Codex Desktop内部恢复细节。
