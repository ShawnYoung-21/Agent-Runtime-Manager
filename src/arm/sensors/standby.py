"""待机/合盖传感（T4）。

本机实测结论（2026-09-23，Win11 build 26200）：
- 仅支持 **S0 低功耗待机（联网），无 S3** → 是 Modern Standby 设备。
- 电源方案"电源按钮和盖子"子组中**没有"合上盖子"(LIDACTION) 设置项**——
  S0 设备通常隐藏 LID 动作，合盖行为由系统接管。
- 推论：不能依赖 powercfg 读写 LIDACTION 来感知/控制合盖；S0 下的保活要靠
  ES_AWAYMODE_REQUIRED / 禁进程 Power Throttling（见 control/，Architecture §5.3）。

本模块提供（均为幂等查询，可测）：
  sleep_states()            powercfg /a 原文
  supports_modern_standby() 是否 S0（Modern Standby）
  supports_s3()             是否传统 S3 睡眠
  has_battery()             是否有电池（笔记本 → 才有"合盖电池"场景）
  has_lid_action_setting()  方案里是否暴露 LIDACTION（S0 机通常 False）

WM_POWERBROADCAST 事件监听（挂消息循环）留给 engine 接入，
这里只放 PBT_* 常量与解析，便于单测。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import win32api
except ImportError:
    win32api = None  # type: ignore

# WM_POWERBROADCAST 事件码（engine 用）
PBT_APMSUSPEND = 0x0004          # 系统即将挂起
PBT_APMRESUMESUSPEND = 0x0007    # 从挂起恢复
PBT_APMRESUMEAUTOMATIC = 0x0012  # 自动恢复
PBT_POWERSTATUSCHANGE = 0x000A   # 电源状态变化（AC/电池切换）

PBT_NAMES = {
    PBT_APMSUSPEND: "SUSPEND",
    PBT_APMRESUMESUSPEND: "RESUME",
    PBT_APMRESUMEAUTOMATIC: "RESUME_AUTO",
    PBT_POWERSTATUSCHANGE: "POWER_STATUS_CHANGE",
}


def _run_powercfg(args: list[str]) -> str:
    """调 powercfg，返回 stdout（GBK 解码）。

    注意：须经 PowerShell/cmd 语义调用——Git Bash(MSYS) 会把 '/x' 参数误转为路径。
    本函数用 subprocess 直接调 powercfg.exe，不经 shell，规避该问题。
    """
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powercfg", *args], capture_output=True, timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW：后台轮询时控制台程序不弹窗
        )
        return out.stdout.decode("gbk", errors="replace") if out.stdout else ""
    except Exception:
        return ""


def sleep_states() -> str:
    """powercfg /a 原文（doctor 展示支持的待机模型）。"""
    return _run_powercfg(["/a"]).strip()


def _available_states_block(out: str) -> str:
    """powercfg /a 分"此系统上有"与"此系统上没有"两段；只取"上有"段。"""
    if "没有" in out:
        head, _, _ = out.partition("此系统上没有")
        return head
    return out


def supports_modern_standby() -> Optional[bool]:
    """是否支持 S0 低功耗待机（Modern Standby）。查不到返回 None。"""
    out = sleep_states()
    if not out:
        return None
    avail = _available_states_block(out)
    return ("S0" in avail) and ("待机" in avail or "Standby" in avail)


def supports_s3() -> Optional[bool]:
    """是否支持传统 S3 睡眠（只看"上有"段，排除"没有/禁用"）。"""
    out = sleep_states()
    if not out:
        return None
    avail = _available_states_block(out)
    return "S3" in avail


def has_battery() -> bool:
    """是否有电池（笔记本）。无电池设备不存在"合盖电池"场景。"""
    if win32api is None:
        return False
    try:
        flag = win32api.GetSystemPowerStatus().get("BatteryFlag", 255)
        return not bool(flag & 128)  # 128 = no system battery
    except Exception:
        return False


def has_lid_action_setting() -> Optional[bool]:
    """当前方案是否暴露"合上盖子"(LIDACTION) 设置。S0 设备通常为 False。"""
    out = _run_powercfg(["/query", "SCHEME_CURRENT",
                         "4f971e89-eebd-4455-a8de-9e59040e7347"])  # SUB_BUTTONS
    if not out:
        return None
    # LIDACTION GUID 或"合上盖子"/"闭合"字样出现才算暴露
    return ("5ca83367-6e45-459f-a27b-476b1d01c936" in out) or ("合" in out and "盖" in out)


def pbt_name(wparam: int) -> str:
    return PBT_NAMES.get(wparam, f"UNKNOWN(0x{wparam:04x})")


@dataclass(frozen=True)
class StandbyInfo:
    modern_standby: Optional[bool]
    s3: Optional[bool]
    has_battery: bool
    has_lid_action: Optional[bool]


def info() -> StandbyInfo:
    """汇总待机相关信息（供 status/doctor 展示）。"""
    return StandbyInfo(
        modern_standby=supports_modern_standby(),
        s3=supports_s3(),
        has_battery=has_battery(),
        has_lid_action=has_lid_action_setting(),
    )
