"""T9 状态机 + 策略测试（纯逻辑，对应 Architecture §6）。"""

from __future__ import annotations

from arm.engine.policy import decide
from arm.engine.state_machine import (
    AgentEvent,
    AgentState,
    AgentTracker,
    Protection,
    ProtectionMachine,
    SubState,
)
from arm.sensors.power import PowerSnapshot


def _power(on_battery, pct=80):
    return PowerSnapshot(ac_online=not on_battery, battery_pct=pct, charging=False)


class TestAgentTracker:
    def test_busy_to_idle_to_finish(self):
        t = AgentTracker("s1")
        t.on_event(AgentEvent.BUSY, now=100.0)
        assert t.state == AgentState.RUNNING and t.substate == SubState.BUSY

        t.on_event(AgentEvent.IDLE, now=200.0)
        assert t.substate == SubState.IDLE and t.idle_since == 200.0

        # grace=120s：350 时未超时（差 150→未达 120? 200+120=320，350>320 已超时）
        assert t.maybe_finish(grace_s=120, now=300.0) is False  # 差100<120
        assert t.maybe_finish(grace_s=120, now=321.0) is True   # 差121≥120
        assert t.state == AgentState.FINISHED

    def test_new_prompt_cancels_idle(self):
        t = AgentTracker("s1")
        t.on_event(AgentEvent.IDLE, now=100.0)
        t.on_event(AgentEvent.BUSY, now=150.0)  # 又来一轮
        assert t.substate == SubState.BUSY
        assert t.maybe_finish(grace_s=10, now=1000.0) is False  # 非 idle，不 finish
        assert t.state == AgentState.RUNNING

    def test_stopped(self):
        t = AgentTracker("s1")
        t.on_event(AgentEvent.STOPPED, now=50.0)
        assert t.state == AgentState.STOPPED
        assert t.maybe_finish(grace_s=1, now=999.0) is False  # stopped 不再 finish

    def test_idle_elapsed_only_when_idle(self):
        t = AgentTracker("s1")
        t.on_event(AgentEvent.BUSY, now=0.0)
        assert t.idle_elapsed(now=100.0) == 0.0


class TestProtectionMachine:
    def test_disarmed_stays_disarmed(self):
        p = ProtectionMachine()
        assert p.update(any_agent_active=True) == Protection.DISARMED

    def test_arm_to_protecting_to_armed(self):
        p = ProtectionMachine()
        p.arm()
        assert p.state == Protection.ARMED
        # 有活跃 → PROTECTING
        assert p.update(any_agent_active=True) == Protection.PROTECTING
        # 无活跃 → 回到 ARMED（释放保护但保留开启意图）
        assert p.update(any_agent_active=False) == Protection.ARMED

    def test_disarm(self):
        p = ProtectionMachine()
        p.arm()
        p.update(any_agent_active=True)
        p.disarm()
        assert p.state == Protection.DISARMED


class TestPolicy:
    def test_disarmed_never_protects(self):
        d = decide(Protection.DISARMED, any_agent_active=True, power=_power(True))
        assert d.should_protect is False

    def test_armed_no_agent_no_protect(self):
        d = decide(Protection.ARMED, any_agent_active=False, power=_power(True))
        assert d.should_protect is False

    def test_armed_with_agent_protects(self):
        d = decide(Protection.PROTECTING, any_agent_active=True, power=_power(True))
        assert d.should_protect is True
        assert "电池" in d.reason

    def test_network_down_warns_but_still_protects(self):
        d = decide(Protection.PROTECTING, any_agent_active=True,
                   power=_power(True), network_up=False)
        assert d.should_protect is True
        assert any("网络" in w for w in d.warnings)

    def test_low_battery_warns(self):
        d = decide(Protection.PROTECTING, any_agent_active=True,
                   power=_power(True, pct=15))
        assert any("电量低" in w for w in d.warnings)
