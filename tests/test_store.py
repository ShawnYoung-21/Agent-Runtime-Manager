"""T2 存储 + 单实例锁 + hook 落库链路测试。全部用临时目录，不碰真实 ~/.arm。"""

from __future__ import annotations

import io
import json

import pytest

from arm.adapters import hook_ingress
from arm.core.single_instance import SingleInstanceError, single_instance
from arm.core.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "test.db")


class TestStore:
    def test_record_and_read_agent_event(self, store):
        store.record_agent_event(
            source="claude_hook", event="Stop", semantic="IDLE",
            session_id="s1", cwd="C:/x", payload={"k": 1},
        )
        rows = store.recent_agent_events()
        assert len(rows) == 1
        assert rows[0]["event"] == "Stop"
        assert rows[0]["semantic"] == "IDLE"
        assert rows[0]["session_id"] == "s1"

    def test_upsert_agent_state_overwrites(self, store):
        store.upsert_agent_state(session_id="s1", source="claude_hook", state="RUNNING", substate="busy")
        store.upsert_agent_state(session_id="s1", source="claude_hook", state="RUNNING", substate="idle")
        agents = store.get_agent_states()
        assert len(agents) == 1
        assert agents[0]["substate"] == "idle"

    def test_upsert_preserves_cwd_when_null(self, store):
        """Stop 等事件不带 cwd，upsert 不应把已有 cwd 覆盖成 NULL。"""
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy", cwd="C:/proj")
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="idle", cwd=None)
        assert store.get_agent_states()[0]["cwd"] == "C:/proj"

    def test_protection_roundtrip(self, store):
        assert store.get_protection() is None
        store.set_protection("PROTECTING", reason="agent busy on battery")
        prot = store.get_protection()
        assert prot["state"] == "PROTECTING"
        assert "battery" in prot["reason"]

    def test_env_event(self, store):
        store.record_env_event(kind="power", detail="on_battery")
        # 无异常即可；读取校验留给 status/engine


class TestSingleInstance:
    def test_second_instance_fails(self, tmp_path):
        lock = tmp_path / "x.lock"
        with single_instance("t", lock_file=lock):
            with pytest.raises(SingleInstanceError):
                with single_instance("t", lock_file=lock):
                    pass

    def test_second_instance_fails_cross_process(self):
        """跨进程互斥（回归 2026-09-28 双引擎风暴根源）。

        旧实现用 windll 直调 GetLastError()，ctypes 内部调用冲掉线程错误码，
        第二进程读到 0 误判"无人持锁"双双运行。子进程持锁、本进程再取必须
        报 SingleInstanceError——同进程嵌套测不出该缺陷，必须真跨进程。
        """
        import subprocess
        import sys

        if sys.platform != "win32":
            pytest.skip("Windows 命名互斥体专属")
        code = (
            "import ctypes, time\n"
            "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
            "k.CreateMutexW(None, False, 'Local\\\\arm-si-xproc-mutex')\n"
            "print('held', flush=True)\n"
            "time.sleep(5)\n"
        )
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        try:
            p.stdout.readline()
            with pytest.raises(SingleInstanceError):
                with single_instance("si-xproc"):
                    pass
        finally:
            p.kill()
            p.wait()

    def test_lock_released_after_exit(self, tmp_path):
        lock = tmp_path / "y.lock"
        with single_instance("t", lock_file=lock):
            pass
        # 退出后应能再次获取
        with single_instance("t", lock_file=lock) as ok:
            assert ok is True


class TestHookIngress:
    """模拟 Claude 通过 stdin 喂 JSON，验证落库到注入的临时 store。"""

    def _feed(self, monkeypatch, store, event_opt: str, payload: dict):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        hook_ingress.run(event_opt, store=store)

    def test_user_prompt_submit_marks_busy(self, monkeypatch, store):
        self._feed(monkeypatch, store, "UserPromptSubmit",
                   {"hook_event_name": "UserPromptSubmit", "session_id": "s9", "cwd": "C:/p"})
        agents = store.get_agent_states()
        assert agents[0]["state"] == "RUNNING"
        assert agents[0]["substate"] == "busy"

    def test_stop_marks_idle(self, monkeypatch, store):
        self._feed(monkeypatch, store, "Stop",
                   {"hook_event_name": "Stop", "session_id": "s9", "stop_hook_active": False})
        assert store.get_agent_states()[0]["substate"] == "idle"

    def test_session_end_marks_stopped(self, monkeypatch, store):
        self._feed(monkeypatch, store, "SessionEnd",
                   {"hook_event_name": "SessionEnd", "session_id": "s9"})
        assert store.get_agent_states()[0]["state"] == "STOPPED"

    def test_unknown_event_only_logs(self, monkeypatch, store):
        self._feed(monkeypatch, store, "Notification",
                   {"hook_event_name": "Notification", "session_id": "s9", "message": "hi"})
        # 不产生 agent_state
        assert store.get_agent_states() == []
        # 但事件流水里有
        assert store.recent_agent_events()[0]["event"] == "Notification"

    def test_bad_stdin_never_raises(self, monkeypatch, store):
        monkeypatch.setattr("sys.stdin", io.StringIO("{{{{not json"))
        hook_ingress.run("Stop", store=store)  # 不抛异常


class TestPrune:
    def test_prune_removes_old_keeps_recent_and_running(self, store):
        """历史清理：事件/环境事件留 30 天，终态会话留 14 天，RUNNING 绝不动。"""
        import time as _t

        old = _t.time() - 40 * 86400
        recent = _t.time() - 100
        store.record_agent_event(source="s", event="Stop", session_id="a", ts=old)
        store.record_agent_event(source="s", event="Stop", session_id="b", ts=recent)
        store.record_env_event(kind="power", detail="x", ts=old)
        store.upsert_agent_state(session_id="dead", source="s", state="STOPPED", ts=old)
        store.upsert_agent_state(session_id="done", source="s", state="FINISHED", ts=old)
        store.upsert_agent_state(session_id="live", source="s", state="RUNNING",
                                 substate="busy", ts=old)
        r = store.prune()
        assert r["events"] == 1 and r["env_events"] == 1 and r["terminal_sessions"] == 2
        ids = {a["session_id"] for a in store.get_agent_states()}
        assert ids == {"live"}  # RUNNING 老会话绝不清理
