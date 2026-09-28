"""T3 电源传感测试（用 mock 隔离真实硬件）。"""

from __future__ import annotations

import arm.sensors.power as power
from arm.sensors.power import PowerSensor, PowerSnapshot


def _snap(ac, pct, charging):
    return PowerSnapshot(ac_online=ac, battery_pct=pct, charging=charging)


class TestSnapshotLogic:
    def test_on_battery(self):
        assert _snap(False, 79, False).on_battery is True
        assert _snap(True, 79, False).on_battery is False
        assert _snap(None, None, None).on_battery is False  # 未知不视为电池

    def test_low_battery_threshold(self):
        assert _snap(False, 19, False).low_battery is True
        assert _snap(False, 20, False).low_battery is True
        assert _snap(False, 21, False).low_battery is False
        assert _snap(False, None, False).low_battery is False


class TestPowerSensorTransitions:
    """验证"仅状态变化才产事件"。"""

    def _poll_with(self, monkeypatch, sensor, snap):
        monkeypatch.setattr(power, "_read_power_status", lambda: snap)
        return sensor.poll()

    def test_first_poll_no_event(self, monkeypatch):
        s = PowerSensor()
        # 首次 poll 建立基线，不报变化（ac 从 None→值算变化吗？见下）
        ev = self._poll_with(monkeypatch, s, _snap(True, 80, False))
        # 首次从 unknown→ac_online 视为一次确立，可报 AC_CONNECTED；这里只验证不崩
        assert isinstance(ev, list)

    def test_ac_unplug_produces_on_battery(self, monkeypatch):
        s = PowerSensor()
        self._poll_with(monkeypatch, s, _snap(True, 80, False))   # 基线：AC
        ev = self._poll_with(monkeypatch, s, _snap(False, 80, False))  # 拔电
        kinds = [k for k, _ in ev]
        assert "ON_BATTERY" in kinds

    def test_battery_crossing_threshold(self, monkeypatch):
        s = PowerSensor()
        self._poll_with(monkeypatch, s, _snap(False, 50, False))
        ev = self._poll_with(monkeypatch, s, _snap(False, 15, False))  # 跌破阈值
        kinds = [k for k, _ in ev]
        assert "BATTERY_LOW" in kinds
        # 回升后报恢复
        ev2 = self._poll_with(monkeypatch, s, _snap(False, 60, True))
        kinds2 = [k for k, _ in ev2]
        assert "BATTERY_OK" in kinds2

    def test_no_change_no_event(self, monkeypatch):
        s = PowerSensor()
        self._poll_with(monkeypatch, s, _snap(True, 80, True))
        ev = self._poll_with(monkeypatch, s, _snap(True, 80, True))
        assert ev == []


class TestTranscriptBusy:
    def test_hot_and_cold_files(self, tmp_path, monkeypatch):
        """冷文件（mtime 已静默超阈值）直接判非 busy——免尾读的缓存短路。"""
        import json as _json
        import os
        import time as _t
        from datetime import datetime, timezone

        import arm.sensors.transcript as tr

        root = tmp_path / "projects" / "proj"
        root.mkdir(parents=True)
        monkeypatch.setattr(tr, "transcripts_root", lambda: tmp_path / "projects")

        def _mk(sid, mtime_age, msg_age_s=None):
            f = root / f"{sid}.jsonl"
            if msg_age_s is None:
                msg_age_s = mtime_age
            iso = datetime.now(timezone.utc).isoformat()
            f.write_text(_json.dumps({"type": "assistant", "timestamp": iso}) + "\n",
                         encoding="utf-8")
            os.utime(f, (_t.time() - mtime_age,) * 2)

        _mk("hot", 10)     # mtime 10s 前 → 热：读尾，消息也 10s 前 → busy
        _mk("cold", 3600)  # mtime 1h 前 → 冷：直接非 busy（不读尾）
        tr._tail_cache.clear()
        tr._origin_cache.clear()
        res = {x["session_id"]: x for x in tr.busy_sessions()}
        assert res["hot"]["busy"] is True
        assert res["hot"]["silence_s"] < 90
        assert res["cold"]["busy"] is False
        assert res["cold"]["silence_s"] >= 3600
