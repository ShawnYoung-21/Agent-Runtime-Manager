"""arm 冒烟测试：CLI 可用、版本可读、hook-ingress 不崩。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from arm import __version__
from arm.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("protect", "release", "status", "watch", "doctor", "init"):
        assert cmd in result.output


def test_hook_ingress_swallows_bad_stdin(monkeypatch, tmp_path) -> None:
    """hook-ingress 对非法 stdin 也必须不抛异常（用隔离库，不写真实库）。"""
    import io

    from arm.adapters import hook_ingress
    from arm.core.store import Store

    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
    hook_ingress.run("Stop", store=Store(tmp_path / "s1.db"))  # 不应抛异常


def test_hook_ingress_parses_valid_json(monkeypatch, tmp_path) -> None:
    import io

    from arm.adapters import hook_ingress
    from arm.core.store import Store

    payload = {"hook_event_name": "Stop", "session_id": "abc", "cwd": "C:/x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    store = Store(tmp_path / "s2.db")
    hook_ingress.run("Stop", store=store)  # 不应抛异常
    assert store.get_agent_states()[0]["session_id"] == "abc"


def test_hook_ingress_default_writes_nothing(monkeypatch, capsys) -> None:
    """未注入 store 且未 allow_real 时，不得写任何库（防测试污染生产）。"""
    import io

    from arm.adapters import hook_ingress

    payload = {"hook_event_name": "Stop", "session_id": "zzz", "cwd": "C:/x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hook_ingress.run("Stop")  # 应只打印 skip，不落库
    assert "skip" in capsys.readouterr().out.lower()
