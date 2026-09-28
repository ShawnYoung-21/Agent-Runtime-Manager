"""T15 电源策略组合拳测试（mock Win32，验证快照/钉住/恢复/崩溃自愈）。"""

from __future__ import annotations

import json

import pytest

from arm.control import power_policy as pp


@pytest.fixture()
def fake_readings(monkeypatch):
    """模拟的电源原值。"""
    vals = {
        "unattend_sleep": (120, 120),
        "standby_idle": (0, 0),
        "hibernate_idle": (0, 57600),
        "video_idle": (0, 0),
    }
    state = {"current": dict(vals), "pinned": False}

    def fake_read(sub, gid):
        name = pp.POLICY_SETTINGS_MAP[(sub, gid)]
        if state["pinned"]:
            return (0, 0)
        return state["current"][name]

    def fake_write(sub, gid, ac, dc):
        name = pp.POLICY_SETTINGS_MAP[(sub, gid)]
        state["current"][name] = (ac, dc)
        state["pinned"] = (ac == 0 and dc == 0)
        return True

    monkeypatch.setattr(pp, "_read_index", fake_read)
    monkeypatch.setattr(pp, "_write_index", fake_write)
    return state


@pytest.fixture()
def backup_path(tmp_path, monkeypatch):
    p = tmp_path / "power_backup.json"
    monkeypatch.setattr(pp, "_backup_path", lambda: p)
    return p


class TestPinRestore:
    def test_apply_snapshots_then_pins(self, fake_readings, backup_path):
        r = pp.apply_pinned()
        assert r["pinned"] is True
        assert r.get("snapshot_count") == 3  # video_idle 不再钉（2026-09-28 复审）
        # 钉住后睡眠三项全 0；关屏保持原值（屏幕正常熄灭，不影响保活）
        for name, _, _ in pp.PIN_SETTINGS:
            assert fake_readings["current"][name] == (0, 0)
        assert fake_readings["current"]["video_idle"] == (0, 0)  # 原值本就是 (0,0)

    def test_restore_returns_originals(self, fake_readings, backup_path):
        pp.apply_pinned()
        r = pp.restore_original()
        assert r["restored"] is True
        assert fake_readings["current"]["unattend_sleep"] == (120, 120)
        assert fake_readings["current"]["hibernate_idle"] == (0, 57600)
        # 快照文件应被清理
        assert not backup_path.exists()

    def test_apply_idempotent_no_double_snapshot(self, fake_readings, backup_path):
        """重复 apply 不覆盖真正的原值（用户中途改过设置也不丢）。"""
        pp.apply_pinned()
        # 模拟：用户在保护期间手动改了 unattend_sleep（快照不该被覆盖）
        data = json.loads(backup_path.read_text(encoding="utf-8"))
        data["values"]["unattend_sleep"]["ac"] = 999
        backup_path.write_text(json.dumps(data), encoding="utf-8")
        r = pp.apply_pinned()
        assert r.get("already") is True
        data2 = json.loads(backup_path.read_text(encoding="utf-8"))
        assert data2["values"]["unattend_sleep"]["ac"] == 999  # 原快照保留

    def test_restore_without_snapshot_is_noop(self, fake_readings, backup_path):
        r = pp.restore_original()
        assert r["restored"] is True


class TestCrashRecovery:
    def test_recover_stranded_snapshot(self, fake_readings, backup_path, monkeypatch):
        """daemon 硬杀留下"已钉未恢复"快照 → 启动时自动还原。"""
        pp.apply_pinned()  # 钉住 + 留快照
        assert fake_readings["current"]["unattend_sleep"] == (0, 0)
        # 硬杀重启：recover
        r = pp.recover_if_stranded()
        assert r["recovered"] is True
        assert fake_readings["current"]["unattend_sleep"] == (120, 120)
        assert not backup_path.exists()

    def test_recover_noop_when_clean(self, fake_readings, backup_path):
        r = pp.recover_if_stranded()
        assert r["recovered"] is False


class TestPowerButton:
    """电源键钉组（二态模型：app 在=防误按"不动作"，退=还原）。"""

    @pytest.fixture()
    def button_backup(self, tmp_path, monkeypatch):
        p = tmp_path / "power_button_backup.json"
        monkeypatch.setattr(pp, "_button_backup_path", lambda: p)
        return p

    def test_pin_snapshots_then_disables(self, fake_readings, button_backup, monkeypatch):
        # 电源键原动作=睡眠(1)；钉后=不动作(0)
        vals = {"ac": 1, "dc": 1}
        monkeypatch.setattr(pp, "_read_index",
                            lambda s, g: (vals["ac"], vals["dc"]))
        writes = []
        def fake_write(s, g, ac, dc):
            writes.append((ac, dc))
            vals["ac"], vals["dc"] = ac, dc
            return True
        monkeypatch.setattr(pp, "_write_index", fake_write)
        r = pp.pin_power_button()
        assert r["pinned"] is True
        assert writes == [(0, 0)]
        # 幂等：再次 pin 不重复快照
        r2 = pp.pin_power_button()
        assert r2.get("already") is True
        # 还原=写回原值 + 删快照
        r3 = pp.restore_power_button()
        assert r3["restored"] is True
        assert writes[-1] == (1, 1)
        assert not button_backup.exists()

    def test_restore_without_snapshot_noop(self, fake_readings, button_backup):
        r = pp.restore_power_button()
        assert r["restored"] is True

