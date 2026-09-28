"""Agent 生命周期 + 保护 状态机（T9，Architecture §6）。

纯逻辑、可单测，不碰硬件。输入：Agent 语义事件 / 环境快照；输出：状态与保护决策。

两组状态：
  AgentState   RUNNING(busy/idle) / FINISHED / STOPPED
  Protection   DISARMED / ARMED / PROTECTING

关键取舍（Architecture §6.1）：hooks 能区分"一轮响应结束"和"会话结束"，
但无法语义判断"任务真完成" → FINISHED 用 idle 超时（finish_grace_s）兜底。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentState(str, Enum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"


class SubState(str, Enum):
    BUSY = "busy"
    IDLE = "idle"


class Protection(str, Enum):
    DISARMED = "DISARMED"      # 未开启保护
    ARMED = "ARMED"            # 已开启，但暂无 Agent 在跑
    PROTECTING = "PROTECTING"  # 正在保护（Agent 运行中）


# Agent 语义事件（来自 adapters）
class AgentEvent(str, Enum):
    STARTED = "STARTED"
    BUSY = "BUSY"
    IDLE = "IDLE"
    STOPPED = "STOPPED"


@dataclass
class AgentTracker:
    """跟踪单个 agent 会话的状态与 idle 计时。"""

    session_id: str
    state: AgentState = AgentState.RUNNING
    substate: Optional[SubState] = None
    idle_since: Optional[float] = None   # 进入 idle 的时间戳

    def on_event(self, ev: AgentEvent, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if ev == AgentEvent.STARTED:
            self.state, self.substate, self.idle_since = AgentState.RUNNING, SubState.BUSY, None
        elif ev == AgentEvent.BUSY:
            self.state, self.substate, self.idle_since = AgentState.RUNNING, SubState.BUSY, None
        elif ev == AgentEvent.IDLE:
            if self.state == AgentState.RUNNING:
                self.substate, self.idle_since = SubState.IDLE, now
        elif ev == AgentEvent.STOPPED:
            self.state, self.substate, self.idle_since = AgentState.STOPPED, None, None

    def idle_elapsed(self, now: Optional[float] = None) -> float:
        """当前 idle 时长（秒）；非 idle 返回 0。"""
        if self.substate != SubState.IDLE or self.idle_since is None:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, now - self.idle_since)

    def maybe_finish(self, grace_s: float, now: Optional[float] = None) -> bool:
        """idle 超过 grace_s → 置 FINISHED。返回是否发生了转变。"""
        if (
            self.state == AgentState.RUNNING
            and self.substate == SubState.IDLE
            and self.idle_elapsed(now) >= grace_s
        ):
            self.state, self.substate = AgentState.FINISHED, None
            return True
        return False


@dataclass
class ProtectionMachine:
    """保护状态机：根据 agent 是否活跃 + 是否开启保护，决定 DISARMED/ARMED/PROTECTING。"""

    state: Protection = Protection.DISARMED

    def arm(self) -> None:
        if self.state == Protection.DISARMED:
            self.state = Protection.ARMED

    def disarm(self) -> None:
        self.state = Protection.DISARMED

    def update(self, any_agent_active: bool) -> Protection:
        """根据是否有活跃 agent 刷新保护状态，返回当前状态。

        - DISARMED：不动作（用户没开保护）。
        - ARMED + 有活跃 → PROTECTING；无活跃 → 维持 ARMED（等待任务）。
        - PROTECTING + 无活跃 → ARMED（任务结束，释放保护，但保留"开启"意图）。
        """
        if self.state == Protection.DISARMED:
            return self.state
        if any_agent_active:
            self.state = Protection.PROTECTING
        else:
            self.state = Protection.ARMED
        return self.state
