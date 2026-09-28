"""arm init：向 Claude Code 的 settings.json 注入/移除生命周期 hooks（T6）。

安全契约（用户明确要求）：
1. 写入前先生成 diff 供用户过目（preview_hooks / build_merged）。
2. 写入前自动备份原文件为 settings.arm-backup.json。
3. 只动顶层 "hooks" 键里的 arm 条目，绝不触碰其它任何配置。
4. remove_hooks() 精准删除 arm 注入的条目，恢复用户原有 hooks；--undo 用备份整体还原。

hook 命令用全局 arm.exe 的绝对路径，不依赖 PATH，保证 Claude 总能调到。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

# arm 管理的 hook 事件（Architecture §4.3）
HOOK_EVENTS = ["UserPromptSubmit", "Stop", "SessionStart", "SessionEnd"]

# 标记：用于识别并安全移除 arm 注入的条目（arm 命令带引号/路径，故匹配 hook-ingress）
_MARKER = "hook-ingress"


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_backup_path(settings: Path) -> Path:
    return settings.with_name("settings.arm-backup.json")


def suppress_marker_path() -> Path:
    """卸载抑制标记：init --undo 后存在，hooks 哨兵见到即跳过自动修复。

    长驻引擎内存里的 _hooks_seen_ok 学不到"用户卸载了"，标记是跨进程的事实源。
    """
    from arm.core import paths

    return paths.data_dir() / "hooks_suppress"


def arm_command() -> str:
    """返回全局 arm 的 Windows 绝对路径。"""
    if sys.platform == "win32":
        return str(Path.home() / ".local" / "bin" / "arm.exe")
    return str(Path.home() / ".local" / "bin" / "arm")


def _hook_command(event: str) -> str:
    return f'"{arm_command()}" hook-ingress --event {event}'


def build_arm_hooks() -> dict[str, list[dict[str, Any]]]:
    """构造 arm 要注入的 hooks 块。"""
    return {
        ev: [{"hooks": [{"type": "command", "command": _hook_command(ev)}]}]
        for ev in HOOK_EVENTS
    }


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 配置非法时不擅自修改
        raise ValueError(f"settings.json 不是合法 JSON：{path}")


def build_merged(settings: dict[str, Any]) -> dict[str, Any]:
    """把 arm hooks 合并进 settings（只动 hooks 键，幂等：已存在则覆盖为最新）。"""
    merged = dict(settings)
    hooks = dict(merged.get("hooks") or {})
    for ev, val in build_arm_hooks().items():
        # 保留用户在该事件下已有的非 arm 条目，再追加 arm 条目
        existing = [g for g in hooks.get(ev, []) if not _is_arm_group(g)]
        existing.extend(val)
        hooks[ev] = existing
    merged["hooks"] = hooks
    return merged


def _is_arm_group(group: Any) -> bool:
    """判断 hook 组（单个 dict 或组列表）是否含 arm 注入的命令。

    hooks_installed 直接传 hooks[ev]（组列表），remove/merge 传单个组——两者都兼容。
    非 dict 输入（None/list/…）一律视为"非 arm 组"。
    """
    if isinstance(group, list):
        return any(_is_arm_group(g) for g in group)
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []):
        try:
            if _MARKER in (h.get("command") or ""):
                return True
        except AttributeError:
            continue
    return False


def remove_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """从 settings 移除 arm 注入的 hook 条目（不动用户其它 hooks）。"""
    cleaned = dict(settings)
    hooks = dict(cleaned.get("hooks") or {})
    for ev in list(hooks.keys()):
        kept = [g for g in hooks[ev] if not _is_arm_group(g)]
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if hooks:
        cleaned["hooks"] = hooks
    else:
        cleaned.pop("hooks", None)
    return cleaned


def hooks_installed(settings_path: Optional[Path] = None) -> dict:
    """检查 4 个 hook 事件是否都已注入（健康检查）。

    背景：Claude Code/代理工具保存配置时会整体重写 settings.json，
    可能把我们注入的 hooks 键冲掉（2026-09-25 实际发生过）。
    返回 {"installed": bool, "missing": [事件名...]}。
    """
    path = settings_path or default_settings_path()
    try:
        settings = load_settings(path)
    except ValueError:
        return {"installed": False, "missing": list(HOOK_EVENTS)}
    hooks = settings.get("hooks") or {}
    missing = [ev for ev in HOOK_EVENTS if not _is_arm_group(hooks.get(ev))]
    return {"installed": not missing, "missing": missing}


def preview(settings_path: Optional[Path] = None) -> str:
    """生成注入前后 diff（供用户过目）。返回易读文本。"""
    path = settings_path or default_settings_path()
    before = load_settings(path)
    after = build_merged(before)
    before_s = json.dumps(before, ensure_ascii=False, indent=2)
    after_s = json.dumps(after, ensure_ascii=False, indent=2)
    return before_s, after_s


def install(settings_path: Optional[Path] = None, backup: bool = True) -> Path:
    """备份并写入合并后的 settings。返回备份路径（若备份）。"""
    path = settings_path or default_settings_path()
    before = load_settings(path)
    after = build_merged(before)

    path.parent.mkdir(parents=True, exist_ok=True)
    bak: Optional[Path] = None
    if backup and path.exists():
        bak = default_backup_path(path)
        shutil.copy2(path, bak)
    # 重新 init = 用户改主意：清除卸载抑制标记，哨兵恢复执勤
    try:
        suppress_marker_path().unlink(missing_ok=True)
    except Exception:
        pass
    path.write_text(json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bak


def undo(settings_path: Optional[Path] = None) -> bool:
    """用备份整体还原。无备份返回 False。

    还原后必须删备份 + 写抑制标记：否则哨兵按"备份存在=用户 init 过"
    60s 内重新注入，用户永远卸不掉 hooks（2026-09-28 审查发现的对抗 bug）。
    """
    path = settings_path or default_settings_path()
    bak = default_backup_path(path)
    if not bak.exists():
        return False
    shutil.copy2(bak, path)
    try:
        bak.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        suppress_marker_path().write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    return True
