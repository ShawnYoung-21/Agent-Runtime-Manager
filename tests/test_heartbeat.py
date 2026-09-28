"""保护状态心跳过期机制测试（应对 daemon 硬杀导致的状态滞留）。"""

from __future__ import annotations

import time

import pytest

from arm.core.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "hb.db")


def _set_protection_at(store, state, age_s):
    """写入一条 updated_ts 为 age_s 秒前的保护状态。"""
    import sqlite3

    conn = sqlite3.connect(str(store._path))
    conn.execute(
        "INSERT INTO protection (id, state, updated_ts, reason) VALUES (1,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_ts=excluded.updated_ts",
        (state, time.time() - age_s, "test"),
    )
    conn.commit()
    conn.close()


class TestEffectiveProtection:
    def test_fresh_protecting_not_stale(self, store):
        _set_protection_at(store, "PROTECTING", age_s=2)  # 2 秒前，新鲜
        eff = store.get_effective_protection(heartbeat_timeout_s=30)
        assert eff["state"] == "PROTECTING"
        assert eff["stale"] is False

    def test_old_protecting_is_stale(self, store):
        _set_protection_at(store, "PROTECTING", age_s=999)  # 很久前，daemon 已死
        eff = store.get_effective_protection(heartbeat_timeout_s=30)
        assert eff["state"] == "DISARMED"   # 失效归一
        assert eff["stale"] is True

    def test_old_armed_is_stale(self, store):
        _set_protection_at(store, "ARMED", age_s=999)
        eff = store.get_effective_protection(heartbeat_timeout_s=30)
        assert eff["state"] == "DISARMED"
        assert eff["stale"] is True

    def test_disarmed_never_stale(self, store):
        _set_protection_at(store, "DISARMED", age_s=999)  # DISARMED 无心跳也不会误报
        eff = store.get_effective_protection(heartbeat_timeout_s=30)
        assert eff["state"] == "DISARMED"
        assert eff["stale"] is False

    def test_empty_protection(self, store):
        eff = store.get_effective_protection()
        assert eff["state"] == "DISARMED"
        assert eff["stale"] is False

    def test_raw_protection_untouched(self, store):
        """get_protection（原始读）不受影响，仍返回库里的原值。"""
        _set_protection_at(store, "PROTECTING", age_s=999)
        raw = store.get_protection()
        assert raw["state"] == "PROTECTING"  # 原始读不做过期判断
