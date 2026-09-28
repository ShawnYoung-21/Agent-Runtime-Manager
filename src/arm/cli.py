"""arm CLI 入口。

命令集对应 Task_Breakdown T12。MVP 阶段先落地骨架，
具体保护/感知逻辑在 engine/adapters/sensors/control 各层实现后接入。
"""

from __future__ import annotations

import typer

from arm import __version__

app = typer.Typer(
    name="arm",
    help="Agent Runtime Manager — 让系统根据 Agent 生命周期智能管理运行环境。",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"arm {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback, is_eager=True,
        help="显示版本并退出",
    ),
) -> None:
    """Agent Runtime Manager 命令行。"""


@app.command()
def protect() -> None:
    """开启 Runtime 保护模式（AC3）。

    在当前进程内做一次 arm+tick：若有活跃 Agent 则立即进入保活。
    持续保护需 `arm daemon` 长驻（见 daemon 命令）。
    """
    from arm.engine.runtime import RuntimeEngine

    eng = RuntimeEngine()
    eng.arm()
    r = eng.tick()
    typer.echo(f"保护状态: {r['protection']}  (guard={'保活中' if r['guard_active'] else '未保活'})")
    typer.echo(f"说明: {r['reason']}")
    for w in r["warnings"]:
        typer.echo(f"警告: {w}")
    if not r["any_agent_active"]:
        typer.echo("提示: 当前无活跃 Agent。运行 `arm daemon` 可在 Agent 启动时自动进入保护。")


@app.command()
def release() -> None:
    """释放保护，恢复正常电源管理（AC5）。"""
    from arm.engine.runtime import RuntimeEngine

    eng = RuntimeEngine()
    eng.release()
    typer.echo("保护状态: DISARMED（已释放，恢复正常电源管理）")


@app.command()
def daemon() -> None:
    """长驻引擎：持续感知 Agent 生命周期并自动保活/释放（单实例）。

    这是产品的常驻形态：开着它，Agent 一跑就保护、一停就释放。Ctrl+C 退出。
    """
    from arm.core.single_instance import SingleInstanceError
    from arm.engine.runtime import RuntimeEngine

    eng = RuntimeEngine()
    typer.echo("arm engine 已启动（Ctrl+C 退出并自动释放保护）")
    try:
        eng.run()
    except SingleInstanceError:
        typer.echo("已有 arm engine 实例在运行（单实例锁）。")
    except KeyboardInterrupt:
        typer.echo("\n收到中断，正在释放保护…")
        eng.release()


@app.command()
def ui(
    port: int = typer.Option(8620, "--port", "-p", help="控制台端口"),
) -> None:
    """打开本地 Web 控制台（浏览器访问，仅本机）。"""
    from arm.ui.server import serve

    serve(port=port)


@app.command()
def status() -> None:
    """查看 Agent 与 Runtime 运行状态（AC6）。"""
    from arm.core.store import Store
    from arm.sensors import power, standby

    store = Store()
    prot = store.get_effective_protection()  # 带心跳：daemon 硬杀时自动判失效
    prot_state = prot["state"]
    suffix = "（守护已停止，状态失效）" if prot.get("stale") else ""
    typer.echo(f"保护状态: {prot_state}{suffix}")
    # hooks 健康速查（被第三方工具冲掉时提醒）
    try:
        from arm.core import claude_hooks as _ch
        _hk = _ch.hooks_installed()
        if not _hk["installed"]:
            typer.echo("[!] Claude hooks 缺失（生命周期事件收不到），运行 arm init --yes 修复")
    except Exception:
        pass

    # 环境（实时快照）
    p = power.snapshot()
    s = standby.info()
    env = []
    if p.ac_online is True:
        env.append("电源=AC")
    elif p.ac_online is False:
        env.append(f"电源=电池 {p.battery_pct}%")
    if s.modern_standby:
        env.append("ModernStandby(S0)")
    elif s.s3:
        env.append("S3")
    typer.echo(f"环境:     {' '.join(env) if env else '（未知）'}")

    agents = store.get_agent_states()
    if not agents:
        typer.echo("Agent:    （暂无记录——尚未收到 hook 事件，或 engine 未运行）")
        return
    typer.echo("Agent:")
    for a in agents:
        sub = f"/{a['substate']}" if a["substate"] else ""
        sid = (a["session_id"] or "?")[:8]
        typer.echo(f"  [{a['source']:11}] {a['state']}{sub:7} session={sid} cwd={a['cwd'] or '-'}")


@app.command()
def watch(
    interval: float = typer.Option(2.0, "--interval", "-i", help="刷新间隔秒"),
) -> None:
    """终端实时监视 Agent 与环境状态（Ctrl+C 退出）。"""
    import os
    import time

    from arm.ui.server import _collect_state
    from arm.core.store import Store

    store = Store()
    typer.echo("arm watch — Ctrl+C 退出")
    try:
        while True:
            s = _collect_state(store)
            buf = []
            buf.append(f"保护: {s['protection']['state']}"
                       f"{' (失效)' if s['protection']['stale'] else ''}"
                       f"  电源: {'电池' if s['power']['ac_online'] is False else 'AC'}"
                       f"{' ' + str(s['power']['battery_pct']) + '%' if s['power']['battery_pct'] is not None else ''}"
                       f"  {s['standby']}")
            agents = s["agents"] or []
            if agents:
                for a in agents[:5]:
                    sub = f"/{a['substate']}" if a["substate"] else ""
                    buf.append(f"  [{a['source']:11}] {a['state']}{sub:7} {(a['session_id'] or '-')[:8]} {a['cwd'] or '-'}")
            else:
                buf.append("  （无会话）")
            os.system("cls" if os.name == "nt" else "clear")
            typer.echo("\n".join(buf))
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nwatch 已退出。")


@app.command()
def doctor() -> None:
    """环境自检：Agent 是否可管理、电源/网络能力是否就绪。"""
    import shutil

    from arm.sensors import power, standby

    typer.echo("== arm doctor 环境自检 ==")

    # 0) hooks 注入健康（会被第三方工具重写冲掉）
    from arm.core import claude_hooks as ch
    hk = ch.hooks_installed()
    if hk["installed"]:
        typer.echo("Hooks 注入  : [OK] 4 个事件已在 settings.json")
    else:
        typer.echo(f"Hooks 注入  : [FAIL] 缺失 {hk['missing']} —— 运行 arm init --yes 重新注入")

    # 1) Agent 可管理性
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    typer.echo(f"Claude Code : {('[OK] ' + claude) if claude else '[FAIL] 未找到'}")
    typer.echo(f"Codex CLI   : {('[OK] ' + codex) if codex else '[FAIL] 未找到'}")

    # 2) 电源/待机能力
    p = power.snapshot(include_scheme=True)
    s = standby.info()
    typer.echo(f"电源        : {'AC' if p.ac_online else '电池'} "
               f"{'电量 ' + str(p.battery_pct) + '%' if p.battery_pct is not None else ''} 方案={p.scheme or '-'}")
    standby_model = "Modern Standby (S0)" if s.modern_standby else ("S3" if s.s3 else "未知")
    typer.echo(f"待机模型    : {standby_model}")
    typer.echo(f"笔记本      : {'是' if s.has_battery else '否(无电池)'}")

    # 3) 关键提示
    if s.modern_standby:
        typer.echo("提示        : 本机为 S0 Modern Standby（合盖会冻结前台进程）。")
    if not claude:
        typer.echo("警告        : 未检测到 Claude Code，AC1 暂无法验收")


@app.command(name="init")
def init(
    undo: bool = typer.Option(False, "--undo", help="用备份整体还原 settings.json"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认写入（不带则只预览 diff）"),
) -> None:
    """初始化：向 Claude settings.json 注入生命周期 hooks（AC1 前置）。

    安全约定：只新增/更新顶层 hooks 键里 arm 的条目；写入前自动备份；
    不带 --yes 时只显示 diff，不落盘。
    """
    from arm.core import claude_hooks as ch

    path = ch.default_settings_path()

    if undo:
        if ch.undo(path):
            typer.echo(f"已用备份还原：{path}")
        else:
            typer.echo("未找到备份（settings.arm-backup.json），无法还原。")
        return

    try:
        before_s, after_s = ch.preview(path)
    except ValueError as e:
        typer.echo(f"错误：{e}")
        raise typer.Exit(1)

    typer.echo(f"目标文件: {path}")
    typer.echo("arm 将注入以下 hooks（仅新增/更新 hooks 键，其余配置原样保留）：\n")
    for ev, groups in ch.build_arm_hooks().items():
        cmd = groups[0]["hooks"][0]["command"]
        typer.echo(f"  {ev:16} → {cmd}")

    if not yes:
        typer.echo("\n[预览模式] 未写入。确认无误后运行： arm init --yes")
        return

    bak = ch.install(path, backup=True)
    if bak:
        typer.echo(f"\n已备份原配置 → {bak}")
    typer.echo(f"已注入 hooks → {path}")
    typer.echo("验证：重启 Claude Code 后提交一次 prompt，再 `arm status` 应能看到会话。")



@app.command(name="app")
def desktop_app(
    port: int = typer.Option(8620, "--port", "-p", help="内嵌控制台端口"),
) -> None:
    """ARM 桌面应用（推荐日常形态）：原生窗口 + 托盘常驻，关窗最小化不退出。

    零黑窗：控制台 Python（python.exe）下自动转投 pythonw.exe（GUI 子系统，
    物理上无控制台）重启自身——VBS/计划任务/互护拉起等所有入口一次根治。
    （FreeConsole 方案已试过会把 webview 消息循环弄挂，勿再试。）
    """
    import subprocess
    import sys
    from pathlib import Path

    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            subprocess.Popen(
                [str(pythonw), "-m", "arm.cli", "app", "--port", str(port)],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return  # 控制台父进程退场，黑窗随之消失
    from arm.ui.app import main as app_main

    app_main(port=port)


@app.command(name="hook-ingress", hidden=True)
def hook_ingress(
    event: str = typer.Option(..., "--event", help="hook 事件名，如 UserPromptSubmit/Stop"),
    selftest: bool = typer.Option(False, "--selftest", help="自检模式：只解析不落真实库"),
) -> None:
    """接收 Claude hook 的 stdin JSON 并落库。须毫秒级返回，不阻塞 Claude。"""
    # 由 settings.json 的 hook 命令调用；具体实现在 adapters/hook_ingress.py（T5）
    from arm.adapters import hook_ingress as _ingress

    # allow_real=True：这是 Claude 真实 hook 的生产调用点，允许落真实库
    _ingress.run(event, selftest=selftest, allow_real=True)


@app.command()
def watchdog() -> None:
    """OS 级看门狗（单次运行，供 ARM-Watchdog 计划任务每 5 分钟调用）。

    合并架构下 app 进程 = 引擎宿主：app 硬死（崩溃/被杀）则引擎同死，
    进程内互护全灭——只有任务计划程序（进程外）能拉起。
    三条件同时满足才拉起（克制，宁缺勿滥）：
    1. 用户装过 ARM-App（没装自启的用户不被打扰）
    2. app_heartbeat 过期 >90s
    3. 当前无任何 arm app 进程（活着但心跳停 = 卡死场景，交给引擎内
       _app_watchdog，这里再拉起只会白唤窗口）
    """
    import subprocess
    import sys
    import time
    from pathlib import Path

    import psutil

    from arm.core import paths
    from arm.core.logging_util import get_logger
    from arm.core.store import Store

    hb = paths.data_dir() / "app_heartbeat"
    if not hb.exists():
        return  # 从未跑过 app，不打扰
    try:
        age = time.time() - float(hb.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return
    if age < 90.0:
        return  # app 活着
    # 用户主动退出（托盘"退出"留了标记）→ 尊重，不拉起；
    # 下次启动 app 时标记自动清除，看门狗恢复执勤
    if (paths.data_dir() / "app_quit_flag").exists():
        return
    for p in psutil.process_iter(["cmdline"]):
        try:
            if "arm.cli app" in " ".join(p.info["cmdline"] or []):
                return  # 进程还在：卡死场景，不重复拉起
        except Exception:
            pass
    r = subprocess.run(["schtasks", "/Run", "/TN", "ARM-App"],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    if r.returncode == 0:
        try:
            Store().record_agent_event(
                source="arm_watchdog", event="APP_REVIVED", session_id=None,
                payload={"heartbeat_age_s": round(age)})
        except Exception:
            pass
        get_logger().info("watchdog: app 失联 %.0fs，已经 ARM-App 计划任务拉起", age)
    else:
        get_logger().warning("watchdog: 拉起失败: %s",
                             ((r.stderr or r.stdout) or "").strip()[:120])
    # pythonw 下 sys.stdout 为 None，print 会炸（血泪教训），仅在控制台输出
    if sys.stdout:
        typer.echo(f"[OK] watchdog 完成（app 失联 {age:.0f}s）"
                   if r.returncode == 0 else "[FAIL] watchdog 拉起失败")


if __name__ == "__main__":
    app()
