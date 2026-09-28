"""Claude Code 适配器（T5 收尾）：hooks 精准事件 + 进程探测兜底。

双保险（2026-09-25 用户指出核心场景漏洞）：
  用户"正跑着任务就要拔电合盖走人"，此时会话可能是旧会话（hooks 未加载/被冲掉）。
  只靠 hooks 会在最需要时刻失效 → 必须有进程探测兜底：
    - 扫描系统进程，发现 claude.exe / claude 进程 → 至少 RUNNING/busy（保守）
    - hooks 事件仍是精准信号；两者合并语义见 merge 规则

合并规则（宁可误保护，不可漏保护）：
  - 任一来源说活跃 → 活跃
  - hooks 说 SessionEnd/STOPPED 且进程也消失 → 才算 STOPPED
  - 进程在、hooks 无记录 → RUNNING/busy（保守）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

# Claude Code 进程特征（精准匹配 Code CLI，排除 Desktop 应用/无关文本）
# Code CLI 的决定性特征：命令行含 @anthropic-ai\claude-code 包路径，或 name=claude.exe 且命令行含 "claude" 子命令语义
_CLAUDE_PATTERNS = [
    re.compile(r"@anthropic-ai[/\\]claude-code[/\\]bin[/\\]claude", re.I),  # npm 装的 Code CLI
]

# 排除：Claude Desktop 桌面应用（WindowsApps 里的聊天应用，不是 Code CLI）
_EXCLUDE_PATTERNS = [
    re.compile(r"WindowsApps[/\\]Claude_", re.I),          # Claude Desktop (MSIX)
    re.compile(r"agent-runtime-manager", re.I),            # arm 自身
]


@dataclass(frozen=True)
class DetectedProcess:
    pid: int
    name: str
    cmdline: str
    create_ts: float


def detect_claude_processes() -> list[DetectedProcess]:
    """扫描系统进程，找出 Claude Code 相关进程。

    排除 arm 自身。psutil 不可用时返回空。
    """
    if psutil is None:
        return []
    out: list[DetectedProcess] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            cmdline_parts = info.get("cmdline") or []
            cmdline = " ".join(cmdline_parts)
            if not name and not cmdline:
                continue
            hay = f"{name} {cmdline}"
            # 排除 arm 自身
            if any(p.search(hay) for p in _EXCLUDE_PATTERNS):
                continue
            # 匹配 claude 进程
            if any(p.search(hay) for p in _CLAUDE_PATTERNS):
                out.append(DetectedProcess(
                    pid=info.get("pid") or 0,
                    name=name,
                    cmdline=cmdline[:200],
                    create_ts=info.get("create_time") or 0.0,
                ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def claude_running() -> bool:
    """是否有 Claude Code 进程在跑（轻量布尔版）。"""
    return bool(detect_claude_processes())
