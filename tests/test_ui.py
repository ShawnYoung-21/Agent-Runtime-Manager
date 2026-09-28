"""arm ui 控制台 API 测试（起真 HTTP server，随机端口，隔离库）。"""

from __future__ import annotations

import json
import urllib.request

import pytest

from arm.core.store import Store
from arm.ui.server import _Handler, serve
from http.server import ThreadingHTTPServer


@pytest.fixture()
def server(tmp_path):
    store = Store(tmp_path / "ui.db")
    handler = type("H", (_Handler,), {"store": store})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # 随机端口
    t = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    httpd.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url):
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class TestUIApi:
    def test_index_serves_html(self, server):
        base, _ = server
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            body = r.read().decode("utf-8")
        assert "Agent Runtime Manager" in body and "<html" in body.lower()

    def test_state_shape(self, server):
        base, _ = server
        s = _get(base + "/api/state")
        assert {"now", "protection", "power", "standby", "agents", "events"} <= set(s)
        assert s["protection"]["state"] == "DISARMED"

    def test_protect_release_flow(self, server):
        # 注：本机若真有 Claude Code 进程在跑，进程兜底会让 protect 直接进 PROTECTING；
        # 因此这里只验证 "protect 生效（ARMED 或 PROTECTING）→ release 后 DISARMED" 的闭环。
        base, _ = server
        assert _post(base + "/api/protect")["ok"] is True
        s = _get(base + "/api/state")
        assert s["protection"]["state"] in ("ARMED", "PROTECTING")
        assert _post(base + "/api/release")["ok"] is True
        assert _get(base + "/api/state")["protection"]["state"] == "DISARMED"

    def test_state_reports_claude_procs(self, server):
        base, _ = server
        s = _get(base + "/api/state")
        assert "claude_procs" in s and isinstance(s["claude_procs"], int)

    def test_events_endpoint(self, server):
        base, store = server
        store.record_agent_event(source="claude_hook", event="Stop", session_id="x")
        s = _get(base + "/api/events")
        assert any(e["event"] == "Stop" for e in s["events"])


class TestLidReport:
    """合盖测试报告 API（L5 5.1）。"""

    def test_no_baseline(self, server, tmp_path, monkeypatch):
        # 隔离基线文件路径（不碰真实 ~/.arm）
        from arm.core import paths as pmod

        fake_data = tmp_path / "armdata_nb"
        fake_data.mkdir()
        monkeypatch.setattr(pmod, "data_dir", lambda: fake_data)
        base, _ = server
        s = _get(base + "/api/lid-report")
        assert s["verdict"] == "no_baseline"

    def test_baseline_then_pass(self, server, tmp_path, monkeypatch):
        base, store = server
        # 基线路径指向 tmp（不碰真实 ~/.arm）
        import arm.ui.server as srv
        from arm.core import paths as pmod

        fake_data = tmp_path / "armdata"
        fake_data.mkdir()
        monkeypatch.setattr(pmod, "data_dir", lambda: fake_data)
        # 记录基线
        assert _post(base + "/api/lid-baseline")["ok"] is True
        assert (fake_data / "lid_test_baseline.txt").exists()
        # 产生一条基线后的 hook 事件（模拟合盖期间任务推进）
        store.record_agent_event(source="claude_hook", event="Stop", session_id="s1")
        rep = _get(base + "/api/lid-report")
        assert rep["verdict"] in ("pass", "fail")
        checks = {e["check"] for e in rep["evidence"]}
        assert "daemon 心跳" in checks and "合盖后 hook 事件" in checks


class TestTokenAuth:
    """内嵌服务访问令牌：浏览器仅凭端口不得访问（移除 Web 暴露面）。"""

    def test_no_token_allows_all(self, server):
        base, _ = server
        s = _get(base + "/api/state")  # 未配 token → 放行（测试/兼容）
        assert "protection" in s

    def test_with_token_blocks_browser(self, tmp_path):
        from http.server import ThreadingHTTPServer
        import threading
        import urllib.request
        from arm.core.store import Store
        from arm.ui.server import _Handler

        store = Store(tmp_path / "tok.db")
        handler = type("H", (_Handler,), {"store": store, "token": "SECRET-1"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        # 无 token → 403
        try:
            urllib.request.urlopen(base + "/api/state", timeout=3)
            assert False, "should be blocked"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 对 token → 200
        s = _get(base + "/api/state?t=SECRET-1")
        assert "protection" in s

        # Cookie 载体 → 200（原生窗口首屏 Set-Cookie 后的路径）
        req = urllib.request.Request(base + "/api/state")
        req.add_header("Cookie", "arm_t=SECRET-1")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert r.status == 200

        # 错 token → 403
        try:
            urllib.request.urlopen(base + "/api/state?t=WRONG", timeout=3)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 403

        httpd.shutdown()


class TestNativeAppQuit:
    def test_quit_stops_engine(self):
        """托盘"退出"必须通知引擎收尾：daemon 线程会被进程退出强杀，
        finally 不跑 → 电源钉住"永不"滞留（2026-09-28 退出路径根因）。"""
        from arm.ui.app import NativeApp

        called = []
        app = NativeApp(engine_stop=lambda: called.append(1))
        app._quit()
        assert called == [1]
        assert app._stop.is_set()


class TestNativeAppQuit:
    def _mk_app(self, tmp_path, monkeypatch):
        import arm.ui.app as app_mod
        from arm.core import paths as paths_mod
        from arm.core.store import Store

        monkeypatch.setattr(paths_mod, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(app_mod, "Store", lambda: Store(tmp_path / "a.db"))
        called = []
        app = app_mod.NativeApp(engine_stop=lambda: called.append(1))
        return app, called

    def test_quit_stops_engine_and_leaves_flag(self, tmp_path, monkeypatch):
        """托盘"退出"：通知引擎收尾（daemon 线程会被强杀，finally 不跑→电源滞留）
        + 落退出标记（看门狗尊重用户退出，不自动拉起）。"""
        app, called = self._mk_app(tmp_path, monkeypatch)
        app._quit()
        assert called == [1]
        assert app._stop.is_set()
        assert (tmp_path / "app_quit_flag").exists()

    def test_clear_flag_on_startup(self, tmp_path, monkeypatch):
        """正常启动清除退出标记：看门狗恢复执勤（下次登录自启即覆盖此路径）。"""
        import arm.ui.app as app_mod

        self._mk_app(tmp_path, monkeypatch)
        (tmp_path / "app_quit_flag").write_text("1", encoding="utf-8")
        app_mod._clear_quit_flag()
        assert not (tmp_path / "app_quit_flag").exists()
