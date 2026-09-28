"""保活策略（T9/T11，Architecture §6.3 决策矩阵的纯函数部分）。

输入：保护状态 + 是否有活跃 agent + 环境快照。
输出：PolicyDecision（是否保活 + 原因 + 告警）。

MVP 原则：保护由"agent 活跃 + 用户已 arm"驱动；环境（电池/网络）只影响告警与提示，
不阻止保活（断网可能是临时抖动，PRD 求"先不中断"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from arm.engine.state_machine import Protection
from arm.sensors.power import PowerSnapshot


@dataclass(frozen=True)
class PolicyDecision:
    should_protect: bool
    reason: str
    warnings: tuple[str, ...] = ()


def decide(
    protection: Protection,
    any_agent_active: bool,
    power: Optional[PowerSnapshot] = None,
    network_up: Optional[bool] = None,
) -> PolicyDecision:
    """决策是否保活。

    protection        当前保护状态机状态
    any_agent_active  是否有 RUNNING 的 agent
    power             电源快照（可空）
    network_up        网络是否连通（可空，None=未知）
    """
    if protection == Protection.DISARMED:
        return PolicyDecision(False, "保护未开启(DISARMED)")

    warnings: list[str] = []
    if network_up is False:
        warnings.append("网络断开：Agent 为 API 驱动，任务可能停滞")
    if power is not None and power.on_battery and power.low_battery:
        warnings.append(f"电量低({power.battery_pct}%)：建议接电")

    if not any_agent_active:
        return PolicyDecision(False, "无活跃 Agent（待命中）", tuple(warnings))

    # 有活跃 agent 且已 arm → 保活
    ctx = []
    if power is not None:
        ctx.append("电池" if power.on_battery else "AC")
    return PolicyDecision(
        True,
        "Agent 运行中，进入保活（%s）" % "/".join(ctx) if ctx else "Agent 运行中，进入保活",
        tuple(warnings),
    )
