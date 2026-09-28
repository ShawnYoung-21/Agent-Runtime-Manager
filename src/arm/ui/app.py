"""arm app：原生桌面应用（彻底原生态，非浏览器页面）。

组成（单进程一站式）：
- pywebview 原生窗口（Edge WebView2 渲染现有 UI——三轮打磨的面板零改动搬进窗口）
- 系统托盘（pystray）：状态色图标 + 右键菜单 + 双击/单击唤出窗口
- 内嵌 UI 服务（localhost，仅本机，为窗口供页面）
- 关闭窗口 = 最小化到托盘（不退出）；托盘菜单"退出"才真正退出

与 daemon 的关系：daemon（引擎）是独立进程（开机计划任务），app 通过 SQLite
读状态、通过写库传导 arm/release（复用跨进程同步）。app 挂了不影响守护。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from pathlib import Path

from arm.core.store import Store


class NativeApp:
    """托盘 + 原生窗口。pywebview 的窗口在主线程；托盘在后台线程。"""

    def __init__(self, port: int = 8620, token: Optional[str] = None,
                 engine_stop: Optional[callable] = None) -> None:
        self.port = port
        self.token = token
        self.url = f"http://127.0.0.1:{port}" + (f"?t={token}" if token else "")
        self.store = Store()
        self._window: Optional[object] = None
        self._icon = None
        self._stop = threading.Event()
        self._window_visible = threading.Event()
        self._daemon_dead_since: Optional[float] = None  # 守护监工：daemon 死亡起始时刻
        self._last_autostart: float = 0.0                # 防抖：上次拉起时间
        self._last_show_signal: float = 0.0              # 单实例：二次启动唤出窗口
        self._show_requested = threading.Event()         # 托盘线程 → 主线程 的显示请求
        self._engine_stop = engine_stop                  # 退出时通知引擎线程收尾

    # ---- 状态（与 tray.py 相同的读库逻辑）----
    def _snapshot(self) -> dict:
        eff = self.store.get_effective_protection()
        hb = self.store.heartbeat_age()
        daemon_alive = hb is not None and hb < 35.0
        try:
            from arm.sensors.transcript import busy_sessions

            busy = [x for x in busy_sessions() if x["busy"]]
        except Exception:
            busy = []
        return {"state": eff["state"], "stale": eff["stale"],
                "daemon_alive": daemon_alive, "busy_count": len(busy)}

    def _color_for(self, snap: dict):
        if not snap["daemon_alive"]:
            return (248, 113, 113)
        st = snap["state"]
        if snap["stale"]:
            return (154, 163, 178)
        if st == "PROTECTING":
            return (74, 222, 128)
        if st == "ARMED":
            return (250, 204, 21)
        return (154, 163, 178)

    def _state_text(self, snap: dict) -> str:
        if not snap["daemon_alive"]:
            return "ARM · 守护未运行"
        if snap["stale"]:
            return "ARM · 状态刷新中"
        st = snap["state"]
        if st == "PROTECTING":
            n = snap["busy_count"]
            return f"ARM · 🛡 保护中 · {n} 个任务运行中" if n else "ARM · 🛡 保护中"
        if st == "ARMED":
            return "ARM · 待命中"
        return "ARM · 未保护"

    # ---- 动作 ----
    def start_daemon_and_arm(self, icon=None, item=None) -> None:
        """守护未运行时：先拉起 daemon 进程（计划任务或直接进程），再写布防意图。"""
        import subprocess
        import sys as _sys

        arm_exe = str(Path.home() / ".local" / "bin" / "arm.exe")
        try:
            subprocess.Popen([arm_exe, "daemon"],
                             creationflags=0x08000000,  # CREATE_NO_WINDOW
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            try:
                subprocess.Popen(["schtasks", "/Run", "/TN", "ARM-Daemon"],
                                 creationflags=0x08000000,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        time.sleep(1.5)  # 给 daemon 起身时间
        self.do_protect(icon, item)

    def do_protect(self, icon=None, item=None) -> None:
        # 只写库意图，daemon 两拍内跟随（跨进程同步）——不在托盘进程里实例化引擎
        from arm.engine.state_machine import Protection

        self.store.set_protection(Protection.ARMED.value, reason="user arm (tray)")

    def do_release(self, icon=None, item=None) -> None:
        from arm.engine.state_machine import Protection

        self.store.set_protection(Protection.DISARMED.value, reason="user release (tray)")

    def open_console(self, icon=None, item=None) -> None:
        self.show_window()

    def show_window(self) -> None:
        """唤出原生窗口（永远不开浏览器）。

        线程安全：pywebview 的窗口 API 必须在主线程调——托盘线程只发信号
        （_show_requested），主循环负责 show/重建，然后 win32 强制置前。
        """
        self._show_requested.set()

    def hide_window(self) -> None:
        if self._window is not None:
            try:
                self._window.hide()
                self._window_visible.clear()
            except Exception:
                pass

    def _quit(self, icon=None, item=None) -> None:
        self._stop.set()
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        # 引擎线程必须先收到 stop 让 run() 的 finally 释放保护/还原电源——
        # 若靠进程退出掐死 daemon 线程，finally 不跑，电源会钉死在"永不"滞留
        if self._engine_stop:
            try:
                self._engine_stop()
            except Exception:
                pass
        _write_quit_flag()
        if self._icon:
            self._icon.stop()

    # ---- 托盘（后台线程）----
    def _build_menu(self):
        import pystray

        snap = self._snapshot()
        txt = self._state_text(snap)
        daemon_ok = snap["daemon_alive"] and not snap["stale"]
        return pystray.Menu(
            pystray.MenuItem(lambda item: txt, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开控制台", self.open_console, default=True),
            pystray.MenuItem(
                lambda item: ("启动守护并布防" if not daemon_ok else "开启保护"),
                self.start_daemon_and_arm,
                enabled=lambda item: not daemon_ok or snap["state"] != "PROTECTING"),
            pystray.MenuItem("释放保护", self.do_release,
                             enabled=lambda item: daemon_ok and snap["state"] != "DISARMED"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )

    def _make_icon_image(self, color):
        """品牌盾牌 logo + 右下角状态点（颜色随保护状态）。"""
        from PIL import Image, ImageDraw
        from pathlib import Path as _P

        logo_path = _P(__file__).parent / "assets" / "arm_logo_64.png"
        base = None
        if logo_path.exists():
            try:
                base = Image.open(logo_path).convert("RGBA")
            except Exception:
                base = None
        if base is None:
            # 兜底：纯圆点
            size = 64
            base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(base)
            d.ellipse([4, 4, size-4, size-4], fill=(30, 33, 40, 255))
            d.ellipse([14, 14, size-14, size-14], fill=color + (255,))
            return base
        base = base.resize((64, 64), Image.LANCZOS)
        # 右下角状态圆点（白描边保证可见）
        d = ImageDraw.Draw(base)
        d.ellipse([44, 44, 60, 60], fill=(255, 255, 255, 230))
        d.ellipse([46, 46, 58, 58], fill=color + (255,))
        return base

    def _tray_loop(self) -> None:
        import pystray

        snap = self._snapshot()
        self._icon = pystray.Icon(
            "arm", icon=self._make_icon_image(self._color_for(snap)),
            title=self._state_text(snap), menu=self._build_menu())
        # 双击/左键默认动作=打开控制台
        t = threading.Thread(target=self._refresh_loop, daemon=True)
        t.start()
        self._icon.run()

    def _ensure_daemon(self, snap: dict) -> None:
        """守护监工（合理的自动）：daemon 连续死亡 30 秒 → 自动拉起（60 秒防抖）。

        安全性：
        - daemon 有单实例锁，误拉起的第二个实例会立即自行退出，不会打架
        - 60 秒防抖：拉起失败（如 arm.exe 被删）时不会疯狂重启
        - 30 秒阈值：给正常重启留时间，不误触发
        """
        import logging

        logger = logging.getLogger("arm")
        now = time.time()
        alive = snap["daemon_alive"]
        if alive:
            self._daemon_dead_since = None
            return
        if self._daemon_dead_since is None:
            self._daemon_dead_since = now
            return
        dead_for = now - self._daemon_dead_since
        if dead_for < 30.0:
            return  # 死亡未满 30 秒，观察
        if now - self._last_autostart < 60.0:
            return  # 60 秒内已拉起过，防抖
        try:
            import subprocess
            from pathlib import Path as _P

            arm_exe = _P.home() / ".local" / "bin" / "arm.exe"
            subprocess.Popen(
                [str(arm_exe), "daemon"],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            self._last_autostart = now
            logger.info("app: daemon 连续死亡 %.0fs，已自动拉起", dead_for)
            self.store.record_agent_event(
                source="arm_app", event="DAEMON_AUTOSTARTED", session_id=None,
                payload={"dead_for_s": round(dead_for)})
        except Exception as exc:
            logger.warning("app: 自动拉起 daemon 失败: %s", exc)
            self._last_autostart = now

    def _touch_app_heartbeat(self) -> None:
        """app 心跳文件：daemon 的反向监工依据。"""
        try:
            from arm.core import paths

            hb = paths.data_dir() / "app_heartbeat"
            hb.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    def _check_show_signal(self) -> None:
        """监听"二次启动"信号：新实例写入的时间戳比上次见到的新 → 唤出窗口。"""
        try:
            from arm.core import paths

            sig = paths.data_dir() / "app_show_signal"
            if not sig.exists():
                return
            ts = float(sig.read_text(encoding="utf-8").strip() or 0)
            if ts > self._last_show_signal:
                self._last_show_signal = ts
                self.show_window()
        except Exception:
            pass

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._snapshot()
                self._ensure_daemon(snap)  # 守护监工
                self._touch_app_heartbeat()
                self._check_show_signal()
                if self._icon:
                    self._icon.icon = self._make_icon_image(self._color_for(snap))
                    self._icon.title = self._state_text(snap)
                    self._icon.menu = self._build_menu()
            except Exception:
                pass
            self._stop.wait(3.0)

    # ---- 窗口事件（pywebview 回调）----
    def _on_closed(self) -> None:
        """点窗口 ✕：不退出应用，最小化到托盘。"""
        # pywebview 的 hide 更稳（destroy 后无法再 show）
        # 4.x+ 支持 window.closing 事件拦截；这里用 window.events.closed
        # 之前拦截不了——改用 hide 方案：见 run() 里的 events.shown/closed 绑定
        pass

    # ---- 主循环 ----
    def run(self) -> None:
        import webview

        # 托盘先起（后台线程）
        threading.Thread(target=self._tray_loop, daemon=True).start()

        # 等 UI 服务可用（由 cli.arm app 启动的 serve 线程提供）
        for _ in range(30):
            try:
                import urllib.request

                with urllib.request.urlopen(self.url, timeout=1.0):
                    break
            except Exception:
                time.sleep(0.3)

        def _bind_window_events():
            def _on_shown():
                self._window_visible.set()
                self._force_foreground()

            self._window.events.shown += _on_shown

            def _on_closed():
                # ✕ 关闭 → 窗口销毁后应用仍驻留托盘；点托盘重新 create_window
                self._window = None
                self._window_visible.clear()

            self._window.events.closed += _on_closed

        # 主循环：_show_requested 驱动（托盘/单实例信号都走它）
        # 首次进入即创建窗口；✕ 后回托盘待命，再请求则重建
        # （注意：只在这里 create 一次——此前循环外还建过一扇，会双窗）
        while not self._stop.is_set():
            self._window = webview.create_window(
                "ARM 控制台", self.url,
                width=1020, height=860, min_size=(760, 560),
                background_color="#0f1115",
            )
            _bind_window_events()
            try:
                webview.start(self._noop, gui="edgechromium")
            except Exception:
                webview.start(self._noop)
            # start() 返回 = 窗口被关闭（✕）→ 待命等下一次显示请求
            if self._stop.is_set():
                break
            self._window = None
            self._window_visible.clear()
            self._show_requested.clear()
            while not self._stop.is_set() and not self._show_requested.is_set():
                time.sleep(0.2)
            need_create = True

    def _force_foreground(self) -> None:
        """把窗口强制置到最前（绕过 Windows 前台锁）。

        标准三步：ATTACH 模拟 ALT 键输入解前台锁 → SetForegroundWindow → DETACH。
        失败静默（最坏情况=任务栏闪烁，用户点一下即达）。
        """
        try:
            import win32con
            import win32gui
            import win32process

            if self._window is None:
                return
            # 找到我们窗口的原生句柄：枚举标题匹配的可见窗口
            target = None

            def _cb(hwnd, _):
                nonlocal target
                if win32gui.IsWindowVisible(hwnd) and "ARM 控制台" in (
                        win32gui.GetWindowText(hwnd) or ""):
                    target = hwnd
                    return False
                return True

            win32gui.EnumWindows(_cb, None)
            if not target:
                return
            win32gui.ShowWindow(target, win32con.SW_RESTORE)
            # 解前台锁：给当前前台线程挂 ALT 键状态
            fg = win32gui.GetForegroundWindow()
            fg_thread = 0
            this_tid = win32process.GetCurrentThreadId()
            if fg:
                fg_thread = win32process.GetWindowThreadProcessId(fg)[0]
            win32process.AttachThreadInput(this_tid, fg_thread, True) if fg_thread else None
            try:
                win32gui.SetForegroundWindow(target)
            finally:
                if fg_thread:
                    win32process.AttachThreadInput(this_tid, fg_thread, False)
            win32gui.BringWindowToTop(target)
        except Exception:
            pass  # 最坏=闪烁，不致命

    def _noop(self) -> None:
        pass


def _icon_path():
    """留作 exe 图标定制（pywebview create_window 无 icon 参数）。
    任务栏图标 = arm.exe 的图标，可通过 rcedit 改 exe 资源实现（后续）。"""
    from pathlib import Path as _P

    return _P(__file__).parent / "assets" / "arm.ico"


def _quit_flag_path():
    from arm.core import paths

    return paths.data_dir() / "app_quit_flag"


def _write_quit_flag() -> None:
    """优雅退出落标记：ARM-Watchdog 看到就不拉起（尊重用户退出，2026-09-28 定调）。
    下次正常启动（手动或登录自启）自动清除。"""
    try:
        _quit_flag_path().write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _clear_quit_flag() -> None:
    try:
        _quit_flag_path().unlink(missing_ok=True)
    except Exception:
        pass


def _signal_existing() -> bool:
    """已有 app 实例时：往信号文件写时间戳（已有实例监听），返回 True。"""
    from arm.core import paths
    from pathlib import Path as _P

    sig = paths.data_dir() / "app_show_signal"
    try:
        sig.write_text(str(time.time()), encoding="utf-8")
        return sig.exists()
    except Exception:
        return _P(sig).exists()


def main(port: int = 8620) -> None:
    """arm app 入口：内嵌 UI 服务 + 原生窗口 + 托盘。单实例：二次启动=唤出已有窗口。

    安全：内嵌服务带随机访问令牌——只有本进程创建的原生窗口（URL 含 token）
    能加载页面；浏览器直接开 http://127.0.0.1:8620 一律 403（移除 Web 暴露面）。
    """
    import secrets
    import urllib.request

    from arm.core.single_instance import SingleInstanceError, single_instance

    try:
        _app_lock = single_instance("app")
        _app_lock.__enter__()
    except SingleInstanceError:
        # 已有实例 → 通知它唤出窗口，本进程退出
        _signal_existing()
        return

    _clear_quit_flag()  # 正常启动即清除退出标记：看门狗恢复执勤

    # 电源键钉组（二态模型：app 在=防误按，退=还原）：
    # 先还原上次崩溃可能遗留的快照、再重新钉住——顺序不能反
    try:
        from arm.control import power_policy as _pp

        _pp.restore_power_button()
        _pp.pin_power_button()
    except Exception as exc:
        import logging

        logging.getLogger("arm").warning("电源键钉组失败: %s", exc)

    token = secrets.token_urlsafe(24)

    # 引擎随 app 内嵌（合并链路：不再需要独立 daemon 进程）。
    # 引擎循环在后台线程跑；单实例锁 "engine" 保证全机只有一个引擎
    # （若用户仍手动跑了 arm daemon，这里拿不到锁就跳过，互不干扰）。
    # 线程必须是**非 daemon**：托盘"退出"走 _quit → engine_stop → run() 的
    # finally 释放保护/还原电源；daemon 线程会被进程退出直接掐死，finally 不跑
    # （2026-09-28 电源滞留事故的退出路径根因）。
    engine_ref: dict = {}
    engine_stopped = threading.Event()

    def _run_engine():
        import logging

        from arm.engine.runtime import RuntimeEngine

        try:
            eng = RuntimeEngine()
            engine_ref["eng"] = eng
            if not engine_stopped.is_set():  # 竞态护栏：stop 先到则不进循环
                eng.run()
        except Exception as exc:
            logging.getLogger("arm").warning("embedded engine exited: %s", exc)

    engine_thread = threading.Thread(target=_run_engine)
    engine_thread.start()

    def _stop_engine() -> None:
        engine_stopped.set()
        eng = engine_ref.get("eng")
        if eng is not None:
            eng.stop()

    # 端口策略（用户定调：arm 的残留就该清掉再用，而不是绕开）：
    # 1) 首选端口被占 → 查占用者：
    #    a. 占用者是 arm 自己的进程（命令行含 arm.exe app）→ 杀掉（残留）→ 重占
    #    b. 占用进程已死（TIME_WAIT 残留）→ 直接重占
    #    c. 占用者是无关程序 → 才退避换端口
    import time as _time

    for attempt in range(2):
        occupied_by = _port_owner(port)
        if occupied_by is None:
            break  # 空闲
        pid, cmdline = occupied_by
        is_ours = "arm.exe" in (cmdline or "") and "app" in (cmdline or "")
        proc_alive = _pid_alive(pid)
        if is_ours or not proc_alive:
            try:
                import psutil

                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()
                    _time.sleep(1.0)
            except Exception:
                pass
            logging.getLogger("arm").warning(
                "app: 清理了 8620 端口的残留占用者（pid=%s ours=%s alive=%s）",
                pid, is_ours, proc_alive)
            _time.sleep(0.5)
        else:
            break  # 无关程序占用 → 走退避

    ui_up = False
    tried = []
    for p_try in range(port, port + 10):
        tried.append(p_try)
        url = f"http://127.0.0.1:{p_try}/?t={token}"
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                pass  # 居然通了？不可能（token 每次随机）——视作被占
            continue
        except urllib.error.HTTPError:
            continue  # 有服务但拒绝我们的 token = 别人的服务，换下一个端口
        except Exception:
            pass  # 连接失败 = 端口空闲，用它
        threading.Thread(
            target=_serve_ui_blocking, args=(p_try, token), daemon=True).start()
        for _ in range(25):
            try:
                with urllib.request.urlopen(url, timeout=1.0):
                    ui_up = True
                    break
            except Exception:
                time.sleep(0.3)
        if ui_up:
            port = p_try
            break

    if not ui_up:
        # 兜底：10 个端口都失败，仍用首选端口起服务（可能已有同 token 服务）
        threading.Thread(
            target=_serve_ui_blocking, args=(port, token), daemon=True).start()

    NativeApp(port=port, token=token, engine_stop=_stop_engine).run()
    # 兜底：无论从哪条路径退出主循环，都确保引擎走 finally（释放保护/还原电源）
    _stop_engine()
    engine_thread.join(timeout=10)
    # 退出 = 一切休息：还原电源键动作（睡眠恢复可用）
    try:
        from arm.control import power_policy as _pp2

        _pp2.restore_power_button()
    except Exception:
        pass


def _port_owner(port: int) -> Optional[tuple[int, str]]:
    """查端口占用者：返回 (pid, 命令行摘要)；空闲返回 None。"""
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr.port == port:
                try:
                    p = psutil.Process(c.pid)
                    return (c.pid, " ".join(p.cmdline()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return (c.pid, "")
    except Exception:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return True  # 查不到就当活着（保守）


def _serve_ui_blocking(port: int, token: Optional[str] = None) -> None:
    from arm.ui.server import serve

    serve(port=port, token=token)


if __name__ == "__main__":
    main()
