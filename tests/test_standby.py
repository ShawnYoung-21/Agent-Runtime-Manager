"""T4 待机传感测试（mock powercfg 输出，覆盖 S0/S3 解析）。"""

from __future__ import annotations

import arm.sensors.standby as standby
from arm.sensors.standby import pbt_name

# 本机真实 powercfg /a 输出（S0 联网待机，无 S3）
POWERCFG_A_S0 = """此系统上有以下睡眠状态:
    待机 (S0 低电量待机) 连接的网络
    休眠
    快速启动

此系统上没有以下睡眠状态:
    待机 (S1)
	系统固件不支持此待机状态。

    待机 (S3)
	当支持 S0 低电量待机时，禁用此待机状态。
"""

# 传统 S3 机器
POWERCFG_A_S3 = """此系统上有以下睡眠状态:
    待机 (S3)
    休眠
    混合睡眠

此系统上没有以下睡眠状态:
    待机 (S0 低电量待机)
"""


def _set_a(monkeypatch, text):
    monkeypatch.setattr(standby, "sleep_states", lambda: text)


def test_modern_standby_detected_on_s0(monkeypatch):
    _set_a(monkeypatch, POWERCFG_A_S0)
    assert standby.supports_modern_standby() is True
    assert standby.supports_s3() is False  # S3 在"没有"段，须排除


def test_s3_machine(monkeypatch):
    _set_a(monkeypatch, POWERCFG_A_S3)
    assert standby.supports_s3() is True
    assert standby.supports_modern_standby() is False


def test_empty_output_returns_none(monkeypatch):
    _set_a(monkeypatch, "")
    assert standby.supports_modern_standby() is None
    assert standby.supports_s3() is None


def test_pbt_name():
    assert pbt_name(0x0004) == "SUSPEND"
    assert pbt_name(0x0012) == "RESUME_AUTO"
    assert pbt_name(0x9999).startswith("UNKNOWN")


def test_lid_action_absent_on_s0(monkeypatch):
    # S0 机器：SUB_BUTTONS 查询里没有"合上盖子"设置 → False
    monkeypatch.setattr(standby, "_run_powercfg",
                        lambda args: "子组 GUID: 4f971e89 (电源按钮和盖子)\n  电源设置 GUID: a7066653 (「开始」菜单电源按钮)")
    assert standby.has_lid_action_setting() is False


def test_lid_action_present_when_exposed(monkeypatch):
    monkeypatch.setattr(standby, "_run_powercfg",
                        lambda args: "电源设置 GUID: 5ca83367-6e45-459f-a27b-476b1d01c936 (合上盖子)")
    assert standby.has_lid_action_setting() is True
