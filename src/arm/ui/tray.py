"""系统托盘应用（arm tray）：桌面常驻入口。

形态（用户需求：不要每次开浏览器）：
- 托盘常驻图标，颜色随状态变：绿=保护中 · 黄=待命(ARMED) · 灰=未保护 · 红=守护异常
- 左键单击：打开控制台（系统默认浏览器开 arm ui）
- 右键菜单：打开控制台 / 开启保护 / 释放保护 / 状态文字（动态）/ 退出

架构：托盘进程独立于 daemon（daemon 负责引擎；tray 负责入口和显示）。
tray 从 SQLite 读状态（跨进程共享），操作通过写库/调 engine 传导给 daemon
（复用跨进程同步机制：arm/release 写库 → daemon 两拍内跟随）。

面板：不重写 UI——tray 起一个 arm ui 服务器（若未跑）并打开浏览器标签。
"""

from __future__ import annotations

import threading
import time
import webbrowser
from typing import Optional

from arm.core.store import Store


def _make_icon_image(color: tuple[int, int, int]):
    """品牌盾牌 logo + 右下角状态点。"""
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
        size = 64
        base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(base)
        d.ellipse([4, 4, size-4, size-4], fill=(30, 33, 40, 255))
        d.ellipse([14, 14, size-14, size-14], fill=color + (255,))
        return base
    base = base.resize((64, 64), Image.LANCZOS)
    d = ImageDraw.Draw(base)
    d.ellipse([44, 44, 60, 60], fill=(255, 255, 255, 230))
    d.ellipse([46, 46, 58, 58], fill=color + (255,))
    return base


# 状态色
_COLOR_PROTECTING = (74, 222, 128)   # 绿
_COLOR_ARMED = (250, 204, 21)        # 黄
_COLOR_DISARMED = (154, 163, 178)    # 灰
_COLOR_DEAD = (248, 113, 113)        # 红


class TrayApp:
    def __init__(self, ui_port: int = 8620, ui_url: str = "") -> None:
        self.store = Store()
        self.ui_port = ui_port
        self.ui_url = ui_url or f"http://127.0.0.1:{ui_port}"
        self.icon = None
        self._stop = threading.Event()

    # ---- 状态采集（读库，不抢引擎的活）----
    def _snapshot(self) -> dict:
        eff = self.store.get_effective_protection()
        hb = self.store.heartbeat_age()
        daemon_alive = hb is not None and hb < 35.0
        try:
            from arm.sensors.transcript import busy_sessions

            busy = [x for x in busy_sessions() if x["busy"]]
        except Exception:
            busy = []
        return {
            "state": eff["state"],
            "stale": eff["stale"],
            "daemon_alive": daemon_alive,
            "busy_count": len(busy),
        }

    def _color_for(self, snap: dict):
        if not snap["daemon_alive"]:
            return _COLOR_DEAD
        st = snap["state"]
        if snap["stale"]:
            return _COLOR_DISARMED
        if st == "PROTECTING":
            return _COLOR_PROTECTING
        if st == "ARMED":
            return _COLOR_ARMED
        return _COLOR_DISARMED

    # ---- 动作 ----
    def open_console(self, icon=None, item=None) -> None:
        webbrowser.open(self.ui_url)

    def do_protect(self, icon=None, item=None) -> None:
        # 只写库意图，daemon 两拍内跟随（跨进程同步）——不在托盘进程里实例化引擎
        from arm.engine.state_machine import Protection

        self.store.set_protection(Protection.ARMED.value, reason="user arm (tray)")

    def do_release(self, icon=None, item=None) -> None:
        from arm.engine.state_machine import Protection

        self.store.set_protection(Protection.DISARMED.value, reason="user release (tray)")
        # tray 自己的内存机器无需状态——下次刷新读库即可

    # ---- 托盘 ----
    def _build_menu(self):
        import pystray

        snap = self._snapshot()
        st_text = self._state_text(snap)
        return pystray.Menu(
            pystray.MenuItem(lambda item: st_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开控制台", self.open_console, default=True),
            pystray.MenuItem("开启保护", self.do_protect, enabled=lambda item: snap["state"] != "PROTECTING"),
            pystray.MenuItem("释放保护", self.do_release, enabled=lambda item: snap["state"] != "DISARMED"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )

    def _state_text(self, snap: dict) -> str:
        if not snap["daemon_alive"]:
            return "守护未运行（点'开启保护'可拉起引擎动作）"
        if snap["stale"]:
            return "状态已失效（等待守护刷新）"
        st = snap["state"]
        if st == "PROTECTING":
            n = snap["busy_count"]
            return f"🛡 保护中 · {n} 个任务运行中" if n else "🛡 保护中"
        if st == "ARMED":
            return "待命中（有任务自动保护）"
        return "未保护"

    def _quit(self, icon=None, item=None) -> None:
        self._stop.set()
        if self.icon:
            self.icon.stop()

    def _refresh_loop(self) -> None:
        """后台线程：每 3 秒刷新图标颜色和菜单文字。"""
        while not self._stop.is_set():
            try:
                if self.icon:
                    snap = self._snapshot()
                    self.icon.icon = _make_icon_image(self._color_for(snap))
                    self.icon.menu = self._build_menu()
                    self.icon.title = self._state_text(snap)  # 悬停提示
            except Exception:
                pass
            self._stop.wait(3.0)

    def run(self) -> None:
        import pystray

        snap = self._snapshot()
        self.icon = pystray.Icon(
            "arm",
            icon=_make_icon_image(self._color_for(snap)),
            title=self._state_text(snap),
            menu=self._build_menu(),
        )
        t = threading.Thread(target=self._refresh_loop, daemon=True)
        t.start()
        self.icon.run()


def main(port: int = 8620) -> None:
    """托盘入口：确保 ui 服务在跑，然后常驻托盘。"""
    import urllib.request

    # ui 服务不在则拉起（子进程，随托盘退出不杀——由 daemon 语义管理；简单起见跟随托盘进程树）
    ui_up = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5):
            ui_up = True
    except Exception:
        ui_up = False
    if not ui_up:
        threading.Thread(
            target=_serve_ui_blocking, args=(port,), daemon=True).start()
        # 给 ui 一点启动时间
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0):
                    ui_up = True
                    break
            except Exception:
                time.sleep(0.3)

    TrayApp(ui_port=port).run()


def _serve_ui_blocking(port: int) -> None:
    from arm.ui.server import serve

    serve(port=port)


if __name__ == "__main__":
    main()
