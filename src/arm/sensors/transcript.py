"""Claude transcript 旁路传感（T14，L1 感知层第三信号）。

动机（2026-09-26 首次合盖验收暴露）：
  任务在合盖期间完成（transcript 23:07:42 有完成消息），但 hook 的 Stop 事件
  在 S0 下丢失——arm 库里没有完成记录。hooks 不是 100% 可靠，需要旁路。

原理：
  Claude Code 把每个会话的完整对话流写在
    ~/.claude/projects/<项目目录转义>/<session_id>.jsonl
  每条消息带 UTC timestamp。扫这些文件的 mtime + 末条时间戳即可知道：
    - 会话是否仍活跃（最近 N 秒内有新消息）
    - 会话属于哪个项目（目录名）
    - 会话最后活动时间（即使 hook 丢了也能推断）

三层信号体系（可靠性递增的使用顺序）：
  ① hooks（精准，可能丢）  ② transcript mtime（可靠，旁路）  ③ 进程存在（兜底，粗粒度）
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TranscriptInfo:
    session_id: str          # jsonl 文件名（去 .jsonl）
    project_dir: str         # 项目目录转义名（如 C--Users-<user>-Desktop-Agent-...）
    project_path: str        # 反解出的真实项目路径（尽力）
    mtime: float             # 文件修改时间（unix）
    last_msg_ts: Optional[str]  # 末条消息的 timestamp 原文（UTC ISO），解析失败为 None


def transcripts_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _decode_project_dir(name: str) -> str:
    """把 'C--Users-<user>-Desktop-Agent-Runtime-Manager' 反解为近似显示路径。

    Claude Code 的编码规则：'\\' → '--'，'-'(原名中的连字符) → '-'。
    反解：先按 '--' 分隔（那是原来的路径分隔符），段内 '-' 保持原样。
    仅用于显示，不影响逻辑。
    """
    if not name:
        return name
    parts = name.split("--")
    if len(parts) >= 2 and parts[0].isalpha() and len(parts[0]) == 1:
        # 盘符形式：C -- Users-<user>-Desktop-Agent-Runtime-Manager
        rest = "\\".join(parts[1:])
        return f"{parts[0]}:\\{rest}"
    return name.replace("-", "\\")


def scan_transcripts(max_age_s: float = 86400.0) -> list[TranscriptInfo]:
    """扫描所有 transcript，返回最近 max_age_s 内有活动的会话（默认24h）。

    只看 mtime（快，不读内容）；last_msg_ts 需要读文件尾，单独函数。
    """
    root = transcripts_root()
    if not root.exists():
        return []
    now = time.time()
    out: list[TranscriptInfo] = []
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if now - mt > max_age_s:
                continue
            sid = f.stem
            out.append(TranscriptInfo(
                session_id=sid,
                project_dir=proj_dir.name,
                project_path=_decode_project_dir(proj_dir.name),
                mtime=mt,
                last_msg_ts=None,
            ))
    return out


def read_last_msg_ts(path: Path) -> Optional[str]:
    """读 transcript 尾部，取最后一条带 timestamp 的记录（只读最后 8KB）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
        # 从后往前找最后一个 "timestamp":"..."
        idx = tail.rfind('"timestamp":"')
        if idx == -1:
            return None
        start = idx + len('"timestamp":"')
        end = tail.find('"', start)
        if end == -1:
            return None
        return tail[start:end]
    except OSError:
        return None


def active_sessions(idle_s: float = 180.0) -> list[dict]:
    """最近 idle_s 秒内有活动的会话列表（供引擎/UI 消费）。

    返回按 mtime 新→旧排序：[{session_id, project_path, age_s, mtime}]
    """
    infos = scan_transcripts()
    now = time.time()
    out = []
    for t in infos:
        age = now - t.mtime
        if age <= idle_s:
            out.append({
                "session_id": t.session_id,
                "project_path": t.project_path,
                "age_s": round(age, 1),
                "mtime": t.mtime,
            })
    out.sort(key=lambda x: -x["mtime"])
    return out


def session_finished_grace_elapsed(session_id: str, grace_s: float) -> bool:
    """该会话是否已空闲超过 grace_s（transcript mtime 判定，hook 丢失时的完成自愈）。"""
    root = transcripts_root()
    for proj_dir in root.iterdir():
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            try:
                return (time.time() - f.stat().st_mtime) >= grace_s
            except OSError:
                return False
    return False


def session_origin(session_id: str) -> Optional[str]:
    """判定会话来源：'desktop'（桌面端）/'cli'/None（找不到）。

    依据（实测）：桌面端创建的会话 transcript 早期行含 entrypoint=claude-desktop*
    （如 claude-desktop-3p）；CLI 会话无此标记。
    """
    root = transcripts_root()
    for proj_dir in root.iterdir():
        f = proj_dir / f"{session_id}.jsonl"
        if not f.exists():
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 80:
                        break
                    if "claude-desktop" in line:
                        return "desktop"
                    if '"entrypoint"' in line:
                        try:
                            import json as _json

                            d = _json.loads(line)
                            ep = d.get("entrypoint") or ""
                            if "desktop" in str(ep):
                                return "desktop"
                            return "cli"
                        except Exception:
                            pass
            # 全文扫完没见 entrypoint → CLI（CLI 会话不带该标记）
            return "cli"
        except OSError:
            continue
    return None


# ---- 细粒度忙闲判定（v2：区分"会话存在"与"任务在跑"） ----
# 用户反馈：会话窗口一直开着 ≠ 在跑任务。需要细到"这一轮有没有在干活"。
# transcript 语义（实测修正）：
#   统一规则：最后一条 user/assistant 行距今 silence < FINISH_SILENCE_S → BUSY
#     - 最后是 user：Claude 正在处理（刚发必忙）
#     - 最后是 assistant：刚说完，可能还在继续输出/工具调用
#   silence >= FINISH_SILENCE_S → 本轮已结束 = IDLE
#   实测教训：最后=user 但静默 40 小时的会话（中断的）必须判 IDLE——
#   所以不能只看 type，必须叠加静默阈值。
FINISH_SILENCE_S = 90.0

# 性能（2026-09-28 审查）：本模块被引擎(2s)/托盘(3s)/UI页面(2s)三路高频调用，
# 无缓存时每会话每拍都重复"iterdir找文件+读尾64KB+读头80行判origin"。
# - _tail_cache：尾部解析结果按 (路径, mtime) 缓存，文件没增长不重读
# - _origin_cache：会话来源是终身属性，永久缓存
# - 冷文件短路：mtime 龄期已超过静默阈值 → 末条消息必然更早 → 直接判非 busy，
#   整个尾读都省掉（任意时刻真正 busy 的会话通常只有一两个，这是最大头）
_tail_cache: dict[str, tuple[float, Optional[tuple[str, float]]]] = {}
_origin_cache: dict[str, str] = {}


def _last_line_info(path: Path) -> Optional[tuple[str, float]]:
    """读 transcript 尾部，返回 (最后一条 user/assistant 行的 type, 该行 epoch 秒)。

    按 (路径, mtime) 缓存：文件没增长就不重读。"""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    cached = _tail_cache.get(key)
    if cached is not None and cached[0] == mt:
        return cached[1]
    try:
        import json as _json
        from datetime import datetime

        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))  # 尾部 64KB 足够覆盖多轮
            tail = f.read().decode("utf-8", errors="replace")
        last_type: Optional[str] = None
        last_ts: Optional[float] = None
        for line in tail.splitlines():
            line = line.strip()
            if not line or '"type"' not in line:
                continue
            try:
                d = _json.loads(line)
            except Exception:
                continue
            typ = d.get("type")
            ts = d.get("timestamp")
            if typ in ("user", "assistant") and isinstance(ts, str):
                try:
                    epoch = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                last_type, last_ts = typ, epoch
        result = (last_type, last_ts) if (last_type and last_ts) else None
    except OSError:
        return None
    if len(_tail_cache) > 4096:  # 兜底防膨胀（会话约几百个/月，几乎触不到）
        _tail_cache.pop(next(iter(_tail_cache)))
    _tail_cache[key] = (mt, result)
    return result


def session_busy_state(session_id: str,
                       finish_silence_s: float = FINISH_SILENCE_S,
                       now: Optional[float] = None) -> Optional[dict]:
    """判定某会话"当前这轮"是否在干活。

    返回 {"busy": bool, "last_type": 'user'|'assistant', "silence_s": float}
    找不到 transcript 返回 None。
    """
    root = transcripts_root()
    now = time.time() if now is None else now
    for proj_dir in root.iterdir():
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            info = _last_line_info(f)
            if info is None:
                return None
            last_type, last_epoch = info
            silence = max(0.0, now - last_epoch)
            busy = silence < finish_silence_s  # 无论最后是 user 还是 assistant
            return {"busy": busy, "last_type": last_type, "silence_s": round(silence, 1)}
    return None


def busy_sessions(finish_silence_s: float = FINISH_SILENCE_S,
                  max_age_s: float = 86400.0) -> list[dict]:
    """所有"近期活跃"的会话及忙闲（供引擎/UI）。

    busy = 最后一条 user/assistant 消息距今 < finish_silence_s。
    冷文件（mtime 龄期已超阈值）直接判非 busy：末条消息时间 ≤ mtime，
    真实静默只会更长——跳过尾读；silence 显示用 mtime 近似（偏小，仅展示）。
    返回按 busy 优先、静默时间升序。
    """
    out = []
    now = time.time()
    root = transcripts_root()
    for t in scan_transcripts(max_age_s=max_age_s):
        f = root / t.project_dir / f"{t.session_id}.jsonl"
        mtime_age = now - t.mtime
        if mtime_age >= finish_silence_s:
            st: dict = {"busy": False, "last_type": None,
                        "silence_s": round(mtime_age, 1)}
        else:
            info = _last_line_info(f)
            if info is None:
                continue
            last_type, last_epoch = info
            silence = max(0.0, now - last_epoch)
            st = {"busy": silence < finish_silence_s, "last_type": last_type,
                  "silence_s": round(silence, 1)}
        origin = _origin_cache.get(t.session_id)
        if origin is None:
            origin = session_origin(t.session_id) or "cli"
            _origin_cache[t.session_id] = origin
        out.append({
            "session_id": t.session_id,
            "project": _decode_project_dir(t.project_dir),
            "last_activity": t.mtime,
            "agent": "claude",
            "origin": origin,
            **st,
        })
    return [x for x in out if x["busy"]] + [x for x in out if not x["busy"]]
