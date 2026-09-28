"""电源传感（T3）：AC/电池/电量/电源方案。

数据源（Windows）：
- GetSystemPowerStatus（pywin32）→ AC 在线、电池状态、电量百分比、是否充电
- powercfg /getactivescheme        → 当前电源方案

产出语义事件（写入 env_events，供引擎决策）：
  AC_CONNECTED / ON_BATTERY / BATTERY_LOW / BATTERY_OK / CHARGING

MVP 采用"轮询 + 变化才记事件"：sensors/power.py 暴露 snapshot() 与 poll()，
由引擎周期性调用 poll()，仅在 AC 状态或低电跨越阈值时记事件。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Optional

try:
    import win32api  # pywin32
except ImportError:  # 非 Windows 开发/CI
    win32api = None  # type: ignore

# 电量低于该值视为低电（触发 BATTERY_LOW）
LOW_BATTERY_PCT = 20


@dataclass(frozen=True)
class PowerSnapshot:
    """某一时刻的电源状态。"""

    ac_online: Optional[bool]        # True=接电源 False=电池 None=未知(无电池/台式)
    battery_pct: Optional[int]       # 0-100，None=未知
    charging: Optional[bool]         # 是否充电中
    scheme: Optional[str] = None     # 当前电源方案名
    raw: dict = field(default_factory=dict)

    @property
    def on_battery(self) -> bool:
        return self.ac_online is False

    @property
    def low_battery(self) -> bool:
        return self.battery_pct is not None and self.battery_pct <= LOW_BATTERY_PCT


def _read_power_status() -> PowerSnapshot:
    """调 GetSystemPowerStatus；失败时返回全 None 快照（不抛）。"""
    if win32api is None:
        return PowerSnapshot(None, None, None)
    try:
        s = win32api.GetSystemPowerStatus()
    except Exception:
        return PowerSnapshot(None, None, None)

    # ACLineStatus: 0=offline 1=online 255=unknown
    ac = {0: False, 1: True}.get(s.get("ACLineStatus", 255))
    # BatteryLifePercent: 0-100 或 255 unknown
    pct_raw = s.get("BatteryLifePercent", 255)
    pct = pct_raw if 0 <= pct_raw <= 100 else None
    # BatteryFlag bit 8 = charging；128 = no system battery
    flag = s.get("BatteryFlag", 255)
    no_battery = bool(flag & 128)
    charging = bool(flag & 8) if not no_battery else None
    if no_battery:
        pct = None
    return PowerSnapshot(ac, pct, charging, raw=s)


def read_scheme() -> Optional[str]:
    """读当前电源方案名（powercfg /getactivescheme）。失败返回 None。"""
    try:
        out = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, text=True, timeout=5, encoding="gbk", errors="replace",
            creationflags=0x08000000,  # CREATE_NO_WINDOW：后台轮询时控制台程序不弹窗
        )
        # 形如：电源方案 GUID: xxx (平衡)
        line = out.stdout.strip()
        if "(" in line and ")" in line:
            return line[line.rindex("(") + 1: line.rindex(")")]
        return line or None
    except Exception:
        return None


def snapshot(include_scheme: bool = False) -> PowerSnapshot:
    """取一次电源快照。include_scheme 时附带方案名（稍慢，调 powercfg）。"""
    snap = _read_power_status()
    if include_scheme:
        return PowerSnapshot(
            snap.ac_online, snap.battery_pct, snap.charging,
            scheme=read_scheme(), raw=snap.raw,
        )
    return snap


class PowerSensor:
    """轮询式电源传感：仅在状态变化时产事件。"""

    def __init__(self) -> None:
        self._last_ac: Optional[bool] = None
        self._last_low: Optional[bool] = None
        self._last_charging: Optional[bool] = None

    def poll(self) -> list[tuple[str, str]]:
        """取快照并与上次比较，返回 [(event, detail), ...]。"""
        snap = _read_power_status()
        events: list[tuple[str, str]] = []

        if snap.ac_online is not None and snap.ac_online != self._last_ac:
            events.append((
                "AC_CONNECTED" if snap.ac_online else "ON_BATTERY",
                f"ac_online={snap.ac_online}",
            ))
            self._last_ac = snap.ac_online

        if snap.charging is not None and snap.charging != self._last_charging:
            if snap.charging:
                events.append(("CHARGING", "charging started"))
            self._last_charging = snap.charging

        if snap.battery_pct is not None:
            low = snap.low_battery
            if low != self._last_low:
                events.append((
                    "BATTERY_LOW" if low else "BATTERY_OK",
                    f"battery_pct={snap.battery_pct}",
                ))
                self._last_low = low

        return events
