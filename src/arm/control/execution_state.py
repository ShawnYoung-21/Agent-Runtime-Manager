"""执行状态控制（T10 核心）：阻止系统在 Agent 运行时自动睡眠。

机制（Architecture §5.3）：
- SetThreadExecutionState 是**线程级**标志，线程存活期间生效。
  因此对"长驻 engine 线程"调用以保持；engine 退出须显式清除。
- ES_SYSTEM_REQUIRED  阻止系统自动进入睡眠（重置 idle 计时）。
- ES_AWAYMODE_REQUIRED 媒体/Away Mode：让"合上盖子"场景下仍可继续运行
  （S0 Modern Standby 设备的关键——本机无 LID 设置项，见 sensors/standby.py）。
- ES_CONTINUOUS       让设置持续生效，直到显式清除。

设计：ExecutionStateGuard 持有"当前是否保活"。enter() 保活 / exit() 释放。
所有 Win32 调用失败只记日志、不抛——保活失败不应搞崩 engine。
"""

from __future__ import annotations

import sys
from typing import Optional

# SetThreadExecutionState 标志
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040
ES_DISPLAY_REQUIRED = 0x00000002

if sys.platform == "win32":
    import ctypes

    _kernel32 = ctypes.windll.kernel32
else:
    _kernel32 = None  # type: ignore


class ExecutionStateGuard:
    """RAII 式保活：enter 保活，exit/clear 释放。幂等、可重复进入。"""

    def __init__(self, away_mode: bool = True) -> None:
        self._away = away_mode
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def _set(self, flags: int) -> bool:
        if _kernel32 is None:
            return False
        try:
            prev = _kernel32.SetThreadExecutionState(flags)
            # 返回 0 表示失败
            return prev != 0
        except Exception:
            return False

    def enter(self) -> bool:
        """进入保活（防系统睡眠 + Away Mode）。返回是否成功。"""
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if self._away:
            flags |= ES_AWAYMODE_REQUIRED
        ok = self._set(flags)
        if ok:
            self._active = True
        return ok

    def exit(self) -> bool:
        """释放保活，恢复系统正常电源管理。返回是否成功。"""
        ok = self._set(ES_CONTINUOUS)  # 仅 CONTINUOUS = 清除其它需求标志
        if ok:
            self._active = False
        return ok

    # 上下文管理器协议
    def __enter__(self) -> "ExecutionStateGuard":
        self.enter()
        return self

    def __exit__(self, *exc) -> None:
        self.exit()

    def __del__(self) -> None:  # 兜底：对象销毁时释放
        if self._active:
            try:
                self.exit()
            except Exception:
                pass


def current_flags(away_mode: bool = True) -> int:
    """返回保活应使用的 flags（便于测试与日志）。"""
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if away_mode:
        flags |= ES_AWAYMODE_REQUIRED
    return flags
