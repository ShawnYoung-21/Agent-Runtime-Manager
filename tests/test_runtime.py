"""T11 RuntimeEngine 测试（mock 电源/guard，临时库，验证决策→控制闭环）。"""

from __future__ import annotations

import pytest

import arm.engine.runtime as runtime
from arm.engine.runtime import EngineConfig, RuntimeEngine
from arm.sensors.power import PowerSnapshot


@pytest.fixture()
def store(tmp_path):
    from arm.core.store import Store

    return Store(tmp_path / "eng.db")


class FakeGuard:
    """记录 enter/exit 调用的假 guard。"""

    def __init__(self):
        self.active = False
        self.enters = 0
        self.exits = 0

    def enter(self):
        self.active = True
        self.enters += 1
        return True

    def exit(self):
        self.active = False
        self.exits += 1
        return True


def _engine(store, monkeypatch, on_battery=True):
    g = FakeGuard()
    eng = RuntimeEngine(store=store, config=EngineConfig(poll_interval_s=0.01),
                        guard=g)
    monkeypatch.setattr(runtime.power_sensor, "snapshot",
                        lambda: PowerSnapshot(ac_online=not on_battery, battery_pct=80, charging=False))
    # mock 进程探测：默认无 Claude/Codex 进程与 busy rollout（需要时个别测试再覆盖）
    monkeypatch.setattr(runtime, "_claude_running", lambda: False)
    monkeypatch.setattr(runtime, "_codex_active", lambda: False)
    # mock transcript 忙闲：真扫会命中开发机上正在活跃的真实会话（如本会话）
    import arm.sensors.transcript as _tr

    monkeypatch.setattr(_tr, "busy_sessions", lambda: [])
    # 隔离①：电源策略打桩（真跑会对真机电源动刀）
    import arm.control.power_policy as _pp

    power_calls = {"apply": 0, "restore": 0}

    def fake_apply():
        power_calls["apply"] += 1
        return {"pinned": True, "snapshot_count": 0}

    def fake_restore():
        power_calls["restore"] += 1
        return {"restored": True}

    monkeypatch.setattr(_pp, "apply_pinned", fake_apply)
    monkeypatch.setattr(_pp, "restore_original", fake_restore)
    eng._power_calls = power_calls  # 供测试断言（如 release 必须还原电源）
    # 隔离②：data_dir 重定向到测试临时目录——隔离 hooks 抑制标记等真实数据文件
    # （历史上 _app_watchdog 读真实 app_heartbeat 会拉起真 app，已移除，补丁保留防回归）
    from arm.core import paths as _paths

    monkeypatch.setattr(_paths, "data_dir", lambda: store._path.parent)
    return eng, g


class TestEngineTick:
    def test_disarmed_no_protect(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        r = eng.tick()
        assert r["protection"] == "DISARMED"
        assert g.active is False

    def test_arm_with_active_agent_enters_protect(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        r = eng.tick()
        assert r["protection"] == "PROTECTING"
        assert r["should_protect"] is True
        assert g.active is True and g.enters == 1

    def test_agent_finishes_releases_guard(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        eng.tick()
        assert g.active is True
        # agent 变为 STOPPED（无活跃）→ 释放
        store.upsert_agent_state(session_id="s1", source="claude_hook", state="STOPPED")
        r = eng.tick()
        assert r["protection"] == "ARMED"   # 释放保护但保留开启意图
        assert g.active is False and g.exits == 1

    def test_release_disarms_and_exits(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        eng.tick()
        assert g.active is True
        eng.release()
        assert g.active is False
        prot = store.get_protection()
        assert prot["state"] == "DISARMED"

    def test_release_restores_power_policy(self, store, monkeypatch):
        """release() 必须还原电源快照（回归 2026-09-28：漏还原 → 停机后电源滞留全 0）。

        run() 的 finally 只走 release()、没有下一拍 tick，电源还原不能只
        依赖 _apply 的释放分支。
        """
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        eng.tick()
        assert eng._power_calls["apply"] == 1
        eng.release()
        assert eng._power_calls["restore"] == 1

    def test_tick_persists_protection(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        prot = store.get_protection()
        assert prot["state"] == "PROTECTING"


class TestReapStale:
    """僵尸回收 + idle 超时判完成（#1/#2 修复）。"""

    def _old_ts(self, store, session_id, age_s):
        import sqlite3
        conn = sqlite3.connect(str(store._path))
        conn.execute("UPDATE agent_state SET updated_ts=? WHERE session_id=?",
                     (__import__("time").time() - age_s, session_id))
        conn.commit(); conn.close()

    def test_idle_over_grace_becomes_finished(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="idle")
        self._old_ts(store, "s1", 121)  # idle 超 120s
        eng.tick()
        states = {a["session_id"]: a["state"] for a in store.get_agent_states()}
        assert states["s1"] == "FINISHED"

    def test_idle_under_grace_untouched(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="idle")
        self._old_ts(store, "s1", 60)  # 没超
        eng.tick()
        states = {a["session_id"]: a["state"] for a in store.get_agent_states()}
        assert states["s1"] == "RUNNING"

    def test_busy_over_session_timeout_becomes_stopped(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s2", source="claude_hook",
                                 state="RUNNING", substate="busy")
        self._old_ts(store, "s2", 1801)  # busy 超 30min 无事件
        eng.tick()
        states = {a["session_id"]: a["state"] for a in store.get_agent_states()}
        assert states["s2"] == "STOPPED"

    def test_busy_under_timeout_untouched(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s2", source="claude_hook",
                                 state="RUNNING", substate="busy")
        self._old_ts(store, "s2", 300)  # 5min，正常
        eng.tick()
        states = {a["session_id"]: a["state"] for a in store.get_agent_states()}
        assert states["s2"] == "RUNNING"

    def test_finished_releases_protection(self, store, monkeypatch):
        """idle 超时 → FINISHED → daemon 不再保活（AC5）。"""
        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        eng.arm()
        eng.tick()
        assert g.active is True
        # 任务完成：变 idle 且超时
        store.upsert_agent_state(session_id="s1", source="claude_hook",
                                 state="RUNNING", substate="idle")
        self._old_ts(store, "s1", 121)
        eng.tick()
        assert g.active is False  # 保护已释放


class TestProcessFallback:
    """进程探测兜底（核心场景：旧会话没有 hooks 也能被保护）。"""

    def test_process_detected_protects_even_without_hooks(self, store, monkeypatch):
        """hooks 无记录（旧会话）但 Claude 进程在跑 → protect 应 PROTECTING。"""
        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(runtime, "_claude_running", lambda: True)  # 进程兜底触发
        eng.arm()
        r = eng.tick()
        assert r["protection"] == "PROTECTING"
        assert g.active is True

    def test_process_gone_releases(self, store, monkeypatch):
        """进程消失且无 hooks 记录 → 释放。"""
        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(runtime, "_claude_running", lambda: True)
        eng.arm()
        eng.tick()
        assert g.active is True
        monkeypatch.setattr(runtime, "_claude_running", lambda: False)
        eng.tick()
        assert g.active is False


class TestCrossProcessSync:
    """跨进程一致：用户在别的进程 release，daemon 两拍内必须跟随（修"自己又保护"）。"""

    def test_user_release_followed_by_daemon(self, store, monkeypatch):
        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(runtime, "_claude_running", lambda: True)
        eng.arm()
        eng.tick()
        assert g.active is True   # 保护中

        # 用户在另一进程 release：写库 DISARMED + reason=user release
        store.set_protection("DISARMED", reason="user release")
        # daemon 首拍：检测到库变化 → 采纳释放
        r = eng.tick()
        assert g.active is False, "daemon 必须跟随用户 release"
        # 且不会自己弹回
        r2 = eng.tick()
        assert g.active is False
        assert r2["protection"] == "DISARMED"

    def test_external_arm_adopted(self, store, monkeypatch):
        """daemon DISARMED 时，外部 arm 写库 ARMED → daemon 采纳并保活。"""
        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(runtime, "_claude_running", lambda: True)
        eng.tick()  # daemon 起始 DISARMED
        assert g.active is False
        store.set_protection("ARMED", reason="arm protect")
        eng.tick()
        assert g.active is True, "daemon 必须采纳外部 arm"

    def test_protect_action_logged(self, store, monkeypatch):
        """保护状态变迁写事件流（UI 时间线可见 arm 动作）。"""
        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(runtime, "_claude_running", lambda: True)
        eng.arm()
        eng.tick()
        srcs = [e["source"] for e in
                [dict(r) for r in store.recent_agent_events(10)]]
        assert "arm_engine" in srcs


class TestHooksSentinel:
    """hooks 自动哨兵：第三方工具冲掉 hooks 后 daemon 自动修复。"""

    def _force_check(self, eng, monkeypatch, settings_file):
        """让哨兵立即检查并指向临时 settings。"""
        import arm.engine.runtime as rt
        from arm.core import claude_hooks as ch

        monkeypatch.setattr(ch, "default_settings_path", lambda: settings_file)
        monkeypatch.setattr(eng, "_last_hooks_check", 0.0)
        eng._hooks_seen_ok = True  # 模拟"之前见过在位"
        eng._hooks_sentinel()

    def test_repairs_when_wiped(self, store, monkeypatch, tmp_path):
        eng, _ = _engine(store, monkeypatch)
        settings_file = tmp_path / "settings.json"
        import json
        settings_file.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        self._force_check(eng, monkeypatch, settings_file)
        # 修复后 hooks 应在位
        from arm.core import claude_hooks as ch

        assert ch.hooks_installed(settings_file)["installed"] is True
        # 且动作被记录到事件流
        srcs = [dict(r)["source"] for r in store.recent_agent_events(10)]
        assert "arm_engine" in srcs

    def test_noop_when_healthy(self, store, monkeypatch, tmp_path):
        eng, _ = _engine(store, monkeypatch)
        settings_file = tmp_path / "settings.json"
        import json
        settings_file.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        from arm.core import claude_hooks as ch

        ch.install(settings_file, backup=False)  # 已在位
        self._force_check(eng, monkeypatch, settings_file)
        events = [dict(r)["event"] for r in store.recent_agent_events(10)]
        assert "HOOKS_REPAIRED" not in events

    def test_no_repair_if_never_ok(self, store, monkeypatch, tmp_path):
        """从未 init 过的用户：不擅自动配置。"""
        eng, _ = _engine(store, monkeypatch)
        settings_file = tmp_path / "settings.json"
        import json
        settings_file.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        import arm.engine.runtime as rt
        from arm.core import claude_hooks as ch

        monkeypatch.setattr(ch, "default_settings_path", lambda: settings_file)
        monkeypatch.setattr(eng, "_last_hooks_check", 0.0)
        eng._hooks_seen_ok = False  # 从未见过在位
        eng._hooks_sentinel()
        assert ch.hooks_installed(settings_file)["installed"] is False  # 没被动过


class TestTranscriptActivation:
    """桌面会话保护缺口（2026-09-28）：Desktop 引擎不发 UserPromptSubmit/Stop，
    transcript 忙闲必须成为正式激活信号 + reap 必须让位于热 transcript。"""

    def test_reap_defers_to_hot_transcript(self, store, monkeypatch):
        from types import SimpleNamespace
        import time

        import arm.sensors.transcript as tr

        eng, g = _engine(store, monkeypatch)
        store.upsert_agent_state(session_id="desk1", source="claude_hook",
                                 state="RUNNING", substate="busy")
        TestReapStale._old_ts(self, store, "desk1", 1801)  # 库里心跳已 31min
        monkeypatch.setattr(
            tr, "scan_transcripts",
            lambda **kw: [SimpleNamespace(session_id="desk1", mtime=time.time() - 10)])
        eng.tick()
        a = [x for x in store.get_agent_states() if x["session_id"] == "desk1"][0]
        assert a["state"] == "RUNNING"              # 没被误收僵尸
        assert time.time() - a["updated_ts"] < 60   # 心跳被 transcript 摸新

    def test_transcript_busy_activates_protection(self, store, monkeypatch):
        import arm.sensors.transcript as tr

        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(tr, "busy_sessions",
                            lambda: [{"session_id": "x", "busy": True}])
        eng.arm()
        r = eng.tick()
        assert r["protection"] == "PROTECTING"
        assert r["any_agent_active"] is True
        assert g.active is True

    def test_transcript_idle_alone_not_active(self, store, monkeypatch):
        """transcript 空闲且无 RUNNING 行/进程 → 不保护（无误保护膨胀）。"""
        import arm.sensors.transcript as tr

        eng, g = _engine(store, monkeypatch)
        monkeypatch.setattr(tr, "busy_sessions", lambda: [])
        eng.arm()
        r = eng.tick()
        assert r["protection"] == "ARMED"
        assert r["any_agent_active"] is False


class TestWatchdogCommand:
    """OS 级看门狗（app 全死 = 引擎同死 = 进程内互护全灭，只有任务计划程序能救）。"""

    def _mk_hb(self, tmp_path, age_s):
        hb = tmp_path / "app_heartbeat"
        import time
        hb.write_text(str(time.time() - age_s), encoding="utf-8")
        return hb

    def _patch(self, monkeypatch, tmp_path, processes, schtasks_rc=0):
        import subprocess as _sub
        from types import SimpleNamespace

        import arm.cli as cli_mod
        from arm.core import paths as paths_mod
        from arm.core.store import Store

        monkeypatch.setattr(paths_mod, "data_dir", lambda: tmp_path)
        monkeypatch.setattr("psutil.process_iter",
                            lambda *a, **kw: [SimpleNamespace(info=p) for p in processes])
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            return SimpleNamespace(returncode=schtasks_rc, stdout="", stderr="")

        monkeypatch.setattr(_sub, "run", fake_run)
        test_store = Store(tmp_path / "wd.db")
        monkeypatch.setattr("arm.core.store.Store", lambda: test_store)
        return cli_mod, calls, test_store

    def test_revives_via_scheduled_task(self, tmp_path, monkeypatch):
        self._mk_hb(tmp_path, 600)
        cli_mod, calls, test_store = self._patch(monkeypatch, tmp_path, processes=[])
        r = typer_testing_invoke(cli_mod)
        assert calls["cmd"][:4] == ["schtasks", "/Run", "/TN", "ARM-App"]
        evs = test_store.recent_agent_events(limit=3)
        assert any(dict(e)["event"] == "APP_REVIVED" for e in evs)

    def test_skips_when_heartbeat_fresh(self, tmp_path, monkeypatch):
        self._mk_hb(tmp_path, 5)
        cli_mod, calls, _ = self._patch(monkeypatch, tmp_path, processes=[])
        typer_testing_invoke(cli_mod)
        assert calls == {}

    def test_skips_when_app_process_alive(self, tmp_path, monkeypatch):
        """app 进程还在但心跳停（卡死）→ 不拉起（避免反复唤窗）。"""
        self._mk_hb(tmp_path, 600)
        alive = {"cmdline": ["pythonw.exe", "-m", "arm.cli", "app", "--port", "8620"]}
        cli_mod, calls, _ = self._patch(monkeypatch, tmp_path, processes=[alive])
        typer_testing_invoke(cli_mod)
        assert calls == {}


def typer_testing_invoke(cli_mod):
    from typer.testing import CliRunner

    r = CliRunner().invoke(cli_mod.app, ["watchdog"])
    assert r.exit_code == 0, r.output
    return r


class TestWatchdogQuitFlag:
    def test_skips_when_user_quit(self, tmp_path, monkeypatch):
        """优雅退出留了标记 → 看门狗不拉起（尊重退出，2026-09-28 用户定调）。"""
        import time as _t

        (tmp_path / "app_heartbeat").write_text(str(_t.time() - 600), encoding="utf-8")
        (tmp_path / "app_quit_flag").write_text("1", encoding="utf-8")
        tw = TestWatchdogCommand()
        cli_mod, calls, _ = tw._patch(monkeypatch, tmp_path, processes=[])
        typer_testing_invoke(cli_mod)
        assert calls == {}


class TestHooksSuppress:
    def test_sentinel_skips_when_suppressed(self, store, monkeypatch, tmp_path):
        """init --undo 留下抑制标记 → 哨兵永不自动修（尊重卸载意图）。"""
        import json as _json

        import arm.engine.runtime as rt
        from arm.core import claude_hooks as ch

        eng, _ = _engine(store, monkeypatch)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
        monkeypatch.setattr(ch, "default_settings_path", lambda: settings_file)
        marker = tmp_path / "hooks_suppress"
        marker.write_text("1", encoding="utf-8")
        monkeypatch.setattr(ch, "suppress_marker_path", lambda: marker)
        monkeypatch.setattr(eng, "_last_hooks_check", 0.0)
        eng._hooks_seen_ok = True  # 即便引擎曾见过 OK，标记优先
        eng._hooks_sentinel()
        assert ch.hooks_installed(settings_file)["installed"] is False  # 没被重注入
