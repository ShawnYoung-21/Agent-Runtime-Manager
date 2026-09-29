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


class TestCodexBusy:
    def _reset(self, tr):
        tr._result_cache.update(key=None, at=0.0, data=[])

    def test_global_wal_does_not_mark_latest_catalog_busy(self, monkeypatch):
        """全局 app-server WAL 常热，不得再把最新目录项误判为任务活跃。"""
        import time
        import arm.sensors.codex_transcript as tr

        self._reset(tr)
        monkeypatch.setattr(tr, "runtime_activity_age", lambda: 0.1)
        monkeypatch.setattr(tr, "read_sqlite_timeline", lambda: {})
        monkeypatch.setattr(tr, "scan_rollouts", lambda *_a, **_k: [])
        monkeypatch.setattr(tr, "read_sqlite_catalog", lambda: [{
            "thread_id": "done", "title": "结束任务", "cwd": None,
            "source_kind": "chatgpt", "git_branch": None,
            "source_updated_at": time.time() - 300,
        }])
        row = tr.busy_codex_sessions()[0]
        assert row["busy"] is False
        assert row["activity_source"] == "catalog"

    def test_timeline_terminal_overrides_recent_catalog(self, monkeypatch):
        """明确终态应立即熄灯，不必等待 90 秒。"""
        import time
        import arm.sensors.codex_transcript as tr

        self._reset(tr)
        monkeypatch.setattr(tr, "runtime_activity_age", lambda: 0.1)
        monkeypatch.setattr(tr, "scan_rollouts", lambda *_a, **_k: [])
        monkeypatch.setattr(tr, "read_sqlite_timeline", lambda: {
            "done": {"state": "finished", "reason": "completed", "sequence": 9},
        })
        monkeypatch.setattr(tr, "read_sqlite_catalog", lambda: [{
            "thread_id": "done", "title": "结束任务", "cwd": None,
            "source_kind": "chatgpt", "git_branch": None,
            "source_updated_at": time.time() - 2,
        }])
        row = tr.busy_codex_sessions()[0]
        assert row["busy"] is False
        assert row["activity_source"] == "timeline"
        assert row["finish_reason"] == "completed"

    def test_hot_rollout_overrides_stale_catalog(self, monkeypatch):
        """长任务执行中 catalog 可十几分钟不刷新：热 rollout（mtime 秒级）
        必须覆盖 catalog 判闲，否则真任务熄灯（2026-09-29 回归）。"""
        import time
        import arm.sensors.codex_transcript as tr

        self._reset(tr)
        monkeypatch.setattr(tr, "runtime_activity_age", lambda: 0.1)
        monkeypatch.setattr(tr, "read_sqlite_timeline", lambda: {})
        monkeypatch.setattr(tr, "read_sqlite_catalog", lambda: [{
            "thread_id": "run", "title": "长任务", "cwd": None,
            "source_kind": "vscode", "git_branch": None,
            "source_updated_at": time.time() - 700,   # catalog 静默 11 分钟
        }])
        monkeypatch.setattr(tr, "scan_rollouts", lambda *_a, **_k: [
            {"file": "rollout-x", "session_id": "run", "originator": None,
             "cwd": None, "mtime": time.time() - 15},  # rollout 15s 前还在写
        ])
        rows = {x["session_id"]: x for x in tr.busy_codex_sessions()}
        row = rows["run"]
        assert row["busy"] is True
        assert row["activity_source"] == "rollout"
        assert row["silence_s"] <= 90
        assert row["finish_reason"] is None


class TestCodexRobustness:
    """传感器自身健壮性（2026-09-29 审计：单进程双线程并发 + 头部竞态）。"""

    def test_concurrent_catalog_reads(self, tmp_path, monkeypatch):
        """引擎线程 + UI 线程同时调用：临时文件必须线程级隔离，互不踩踏。"""
        import sqlite3 as sq
        import threading
        import time as _t

        import arm.sensors.codex_transcript as tr

        sql_dir = tmp_path / "sqlite"
        sql_dir.mkdir()
        db = sql_dir / "codex-dev.db"
        conn = sq.connect(db)
        conn.execute(
            "CREATE TABLE local_thread_catalog (thread_id TEXT,"
            " display_title TEXT, source_updated_at REAL, cwd TEXT,"
            " source_kind TEXT, git_branch TEXT)")
        conn.execute("INSERT INTO local_thread_catalog VALUES"
                     " ('t1','任务一',?,NULL,'vscode',NULL)", (_t.time(),))
        conn.commit()
        conn.close()
        (sql_dir / "codex-dev.db-wal").write_bytes(b"")  # 读取键需要 wal 存在
        # sessions_root 语义是 <base>/sessions，读取器取 .parent 找 sqlite/
        monkeypatch.setattr(tr, "sessions_root", lambda: tmp_path / "sessions")
        tr._catalog_cache.clear()
        tr._timeline_cache.clear()

        errors = []
        seen = []

        def worker():
            for _ in range(30):
                try:
                    rows = tr.read_sqlite_catalog()
                    seen.append([r["thread_id"] for r in rows])
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        assert seen and all(s == ["t1"] for s in seen)

    def test_meta_reread_after_partial_header(self, tmp_path, monkeypatch):
        """文件刚创建、session_meta 未落盘时读到半行：不得永久缓存，
        下次扫描必须重读出真实 id（否则会话归属从此错乱）。"""
        import json as _json
        import os
        import time as _t

        import arm.sensors.codex_transcript as tr

        root = tmp_path / "sessions"
        root.mkdir()
        f = root / "rollout-2026-09-29T10-00-00-aaaa-bbbb-cccc-dddd-eeee.jsonl"
        # 第一行不是 meta 也不是 turn_context → _read_meta 标记 _pending
        f.write_text(_json.dumps({"type": "response_item"}) + "\n",
                     encoding="utf-8")
        os.utime(f, (_t.time(),) * 2)  # 新鲜文件（<60s 才触发重读）
        monkeypatch.setattr(tr, "sessions_root", lambda: tmp_path)
        tr._meta_cache.clear()
        tr._tail_cache.clear()

        first = tr.scan_rollouts()[0]
        assert first["session_id"] == "aaaa-bbbb-cccc-dddd-eeee"  # 文件名 id

        with f.open("a", encoding="utf-8") as fh:  # 头部补全
            fh.write(_json.dumps({
                "type": "session_meta",
                "payload": {"session_id": "real-session-id"}}) + "\n")
        second = tr.scan_rollouts()[0]
        assert second["session_id"] == "real-session-id"  # 重读成功


class TestNetworkSensor:
    def test_up_and_cache(self, monkeypatch):
        import arm.sensors.network as net

        calls = []

        class Conn:
            def __enter__(self): return self
            def __exit__(self, *_a): return None

        monkeypatch.setattr(net.socket, "create_connection",
                            lambda *a, **k: calls.append((a, k)) or Conn())
        net._cache.update(at=0.0, data=None)
        first = net.snapshot(cache_s=60)
        second = net.snapshot(cache_s=60)
        assert first["status"] == "up" and second["up"] is True
        assert len(calls) == 1

    def test_timeout_is_down(self, monkeypatch):
        import arm.sensors.network as net

        def fail(*_a, **_k):
            raise net.socket.timeout()

        monkeypatch.setattr(net.socket, "create_connection", fail)
        net._cache.update(at=0.0, data=None)
        result = net.snapshot(cache_s=0)
        assert result["status"] == "down"
        assert "全部探测失败" in result["reason"]

    def test_fallback_to_second_target(self, monkeypatch):
        """首目标（国内 DNS+TCP）超时应回退到下一目标，任一成功即 up。"""
        import arm.sensors.network as net

        real = net.socket.create_connection

        def flaky(address, *a, **k):
            if address[0].startswith("www.baidu.com"):
                raise net.socket.timeout()
            return real(address, *a, **k)

        monkeypatch.setattr(net.socket, "create_connection", flaky)
        net._cache.update(at=0.0, data=None)
        result = net.snapshot(cache_s=0)
        assert result["up"] is True
        assert result["host"] != "www.baidu.com"

    def test_diagnose_ladder(self, monkeypatch):
        """分层诊断：网关信息条不判定、DNS/公网 TCP 为硬判据。"""
        import arm.sensors.network as net

        monkeypatch.setattr(net, "default_gateway", lambda: None)
        monkeypatch.setattr(net.socket, "getaddrinfo",
                            lambda *a, **k: [("2.0.0.0", 0)])
        monkeypatch.setattr(net, "probe",
                            lambda *_a, **_k: {"ok": False, "latency_ms": None, "reason": "TCP 超时"})
        rows = net.diagnose()
        by = {r["check"]: r for r in rows}
        assert by["网关连通(参考)"]["ok"] is None
        assert by["DNS 解析"]["ok"] is True
        assert by["公网 TCP"]["ok"] is False
