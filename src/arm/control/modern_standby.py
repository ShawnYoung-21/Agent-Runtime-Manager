"""进程级防节流（T10，S0 Modern Standby 关键对策）。

背景：S0 联网待机下，Windows 会对"后台/不聚焦"进程做 Power Throttling（EcoQoS），
可能拖慢甚至冻结 Agent 进程。对策：对目标进程设置
PROCESS_POWER_THROTTLING_IGNORE，让它绕过节流。

实现：SetProcessInformation(ProcessPowerThrottling, PROCESS_POWER_THROTTLING_IGNORE)。
- Win10 1709+ 提供；调用失败（旧系统/权限不足）只记日志、不抛。
- 需要 PROCESS_SET_INFORMATION 权限打开目标进程句柄。
"""

from __future__ import annotations

import sys
from typing import Optional

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32
else:
    ctypes = None  # type: ignore
    wintypes = None  # type: ignore
    _kernel32 = None  # type: ignore

PROCESS_SET_INFORMATION = 0x0200
ProcessPowerThrottling = 4                     # PROCESS_INFORMATION_CLASS
PROCESS_POWER_THROTTLING_IGNORE = 1
PROCESS_POWER_THROTTLING_ENABLE = 0  # 恢复默认（跟随系统策略）


class _POWER_THROTTLING_STATE(ctypes.Structure if ctypes else object):  # type: ignore[misc]
    if ctypes:
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]


PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1


def set_ignore_throttling(pid: int, ignore: bool = True) -> bool:
    """对 pid 设置/取消忽略电源节流。返回是否成功（非 Windows 恒 False）。"""
    if _kernel32 is None:
        return False
    handle = None
    try:
        handle = _kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not handle:
            return False
        state = _POWER_THROTTLING_STATE()
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = PROCESS_POWER_THROTTLING_IGNORE
        state.StateMask = PROCESS_POWER_THROTTLING_IGNORE if ignore else 0
        ok = _kernel32.SetProcessInformation(
            handle, ProcessPowerThrottling,
            ctypes.byref(state), ctypes.sizeof(state),
        )
        return bool(ok)
    except Exception:
        return False
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass


def disable_throttling(pid: int) -> bool:
    """让 pid 绕过电源节流（Agent 保护期调用）。"""
    return set_ignore_throttling(pid, ignore=True)


def restore_throttling(pid: int) -> bool:
    """恢复 pid 默认节流（保护释放时调用）。"""
    return set_ignore_throttling(pid, ignore=False)
