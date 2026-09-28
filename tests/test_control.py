"""T10 电源控制测试（mock Win32 层；非 Windows 安全降级）。"""

from __future__ import annotations

import sys

import arm.control.execution_state as es
import arm.control.modern_standby as ms


class TestExecutionStateFlags:
    def test_flags_include_system_and_continuous(self):
        f = es.current_flags(away_mode=True)
        assert f & es.ES_CONTINUOUS
        assert f & es.ES_SYSTEM_REQUIRED
        assert f & es.ES_AWAYMODE_REQUIRED

    def test_flags_away_mode_optional(self):
        f = es.current_flags(away_mode=False)
        assert not (f & es.ES_AWAYMODE_REQUIRED)


class TestGuard:
    def test_enter_exit_toggle_active(self, monkeypatch):
        # mock 掉真实 Win32 调用，只验证状态机
        monkeypatch.setattr(es.ExecutionStateGuard, "_set", lambda self, flags: True)
        g = es.ExecutionStateGuard()
        assert g.active is False
        assert g.enter() is True
        assert g.active is True
        assert g.exit() is True
        assert g.active is False

    def test_enter_failure_stays_inactive(self, monkeypatch):
        monkeypatch.setattr(es.ExecutionStateGuard, "_set", lambda self, flags: False)
        g = es.ExecutionStateGuard()
        assert g.enter() is False
        assert g.active is False

    def test_context_manager(self, monkeypatch):
        monkeypatch.setattr(es.ExecutionStateGuard, "_set", lambda self, flags: True)
        with es.ExecutionStateGuard() as g:
            assert g.active is True
        assert g.active is False


class TestThrottling:
    def test_non_windows_returns_false(self, monkeypatch):
        monkeypatch.setattr(ms, "_kernel32", None)
        assert ms.disable_throttling(1234) is False
        assert ms.restore_throttling(1234) is False

    def test_invalid_pid_safe(self):
        """无效 PID 必须安全返回 False，不抛异常。"""
        if sys.platform != "win32":
            import pytest

            pytest.skip("Windows only")
        assert ms.disable_throttling(999999) is False

    def test_self_process_succeeds_on_windows(self):
        """对本进程设置防节流——真实 Win32 路径（S0 机应支持）。"""
        if sys.platform != "win32":
            import pytest

            pytest.skip("Windows only")
        import os

        assert ms.disable_throttling(os.getpid()) is True
        assert ms.restore_throttling(os.getpid()) is True
