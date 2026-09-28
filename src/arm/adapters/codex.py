"""Codex 适配器（T16）：进程检测 + rollout 旁路。

进程特征（官方包名优先，exe 名不可靠——桌面版 GUI 主进程恰好叫 ChatGPT.exe
但那是 MSIX 包 OpenAI.Codex 的）：
  - codex.exe（CLI / 桌面版运行时 %LOCALAPPDATA%\OpenAI\Codex\bin\*）
  - codex-command-runner.exe（桌面版命令执行器）
  - ChatGPT.exe（仅当路径在 WindowsApps\OpenAI.Codex_* 下才算桌面版 Codex；
    独立安装的 ChatGPT 聊天应用不算——它不是 agent）
"""

from __future__ import annotations

from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

_CODEX_EXE_NAMES = {"codex.exe", "codex-command-runner.exe"}


def _is_codex_cmdline(name: str, cmdline: str) -> bool:
    n = (name or "").lower()
    cl = (cmdline or "").lower()
    if n in _CODEX_EXE_NAMES:
        return True
    # 桌面版 GUI 主进程：ChatGPT.exe 且属于 OpenAI.Codex MSIX 包（路径特征）
    if n == "chatgpt.exe" and "openai.codex_" in cl.replace(" ", ""):
        return True
    return False


def detect_codex_processes() -> list[dict]:
    """扫描 Codex 相关进程。返回 [{pid, name, cmdline}]。"""
    if psutil is None:
        return []
    out = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            cl = " ".join(proc.info.get("cmdline") or [])
            if _is_codex_cmdline(name, cl):
                out.append({"pid": proc.info.get("pid") or 0,
                            "name": name, "cmdline": cl[:200]})
        except Exception:
            continue
    return out


def codex_running() -> bool:
    return bool(detect_codex_processes())
