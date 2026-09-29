"""Codex 会话传感（T16）：双数据源。

【源 A：SQLite 目录】~/.codex/sqlite/codex-dev.db（新版桌面版/VS Code 写入）
  - local_thread_catalog：thread_id / display_title / source_updated_at / cwd /
    source_kind（vscode/chatgpt）/ git_branch —— 会话目录权威来源
  - 忙闲判定：source_updated_at 距今 < 阈值 → BUSY（桌面版实时刷新此字段）
  - 注意：WAL 模式，读取需先复制 db+wal+shm 到临时目录（避免锁冲突/脏读）

【源 B：rollout jsonl】~/.codex/sessions/**/rollout-*.jsonl（CLI 与旧版写入）
  - 行 {timestamp, ordinal, type, payload}；task_started↔task_complete 轮次边界
  - originator=codex_work_desktop → 桌面版创建（旧路径）

两源合并：sqlite 的 thread_id 与 rollout 的 session_id 同空间；去重后输出。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

# 性能（2026-09-28 二轮审查）：本模块被引擎(2s)/UI页面(2s)高频调用，无缓存时
# 每次都"完整复制 sqlite 库 + os.walk 全部 rollout + 逐文件读头判 meta + 读尾64KB"。
# - _meta_cache：rollout 头部不可变（append-only 文件），按路径永久缓存
# - _tail_cache：尾部解析按 (路径, mtime) 缓存，文件没增长不重读
# - 冷文件短路：mtime 龄期已超静默阈值 → 直接判非 busy，整段尾读省掉
# - _catalog_cache：sqlite 目录按 (db mtime, wal mtime) 键缓存，库没变不复制
# - _result_cache：合并结果 2.5s TTL——引擎与 UI 的双调用共享一次真实扫描
#   （调用方契约：不得修改返回的 dict，只读或自行拷贝）
_meta_cache: dict[str, dict] = {}
_tail_cache: dict[str, tuple[float, list]] = {}
_catalog_cache: dict[tuple, list] = {}
_timeline_cache: dict[tuple, dict[str, dict]] = {}
_result_cache: dict = {"key": None, "at": 0.0, "data": []}


def sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _iter_rollouts(max_age_s: float) -> list[Path]:
    root = sessions_root()
    if not root.exists():
        return []
    now = time.time()
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".jsonl") or not fn.startswith("rollout-"):
                continue
            f = Path(dirpath) / fn
            try:
                if now - f.stat().st_mtime <= max_age_s:
                    out.append(f)
            except OSError:
                continue
    return out


def _session_id_of(fname: str) -> str:
    """rollout-2026-09-26T17-53-05-01a0dd21-...jsonl → 尾部 uuid。

    注意：文件名 id 与 session_meta.session_id 不同（子代理是 parent_child 双 id）；
    真正的 session_id 在首行 session_meta。这里先取文件名 uuid 作为文件标识，
    session_id 由 _scan_meta 补全。
    """
    stem = fname[len("rollout-"):-len(".jsonl")]
    # 取最后一段（时间戳后）的 uuid 部分
    parts = stem.split("-")
    if len(parts) >= 5:
        return "-".join(parts[-5:])
    return stem


def _read_meta(f: Path) -> dict:
    """解析 rollout 首行 session_meta（头部不可变，结果进 _meta_cache）。"""
    info = {"session_id": _session_id_of(f.name),
            "originator": None, "cwd": None}
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "session_meta":
                    pl = d.get("payload") or {}
                    if pl.get("session_id"):
                        info["session_id"] = pl["session_id"]
                    info["originator"] = pl.get("originator")
                    info["cwd"] = pl.get("cwd")
                    break
                if d.get("type") == "turn_context":
                    break  # meta 不会出现在 turn_context 之后
    except OSError:
        pass
    return info


def scan_rollouts(max_age_s: float = 7 * 86400.0) -> list[dict]:
    """扫描 rollout 文件，解析首行 session_meta（头部缓存，文件不重读）。

    返回 [{file, session_id, originator, cwd, mtime}]（mtime 新→旧）。
    """
    out = []
    for f in _iter_rollouts(max_age_s):
        key = str(f)
        meta = _meta_cache.get(key)
        if meta is None:
            meta = _read_meta(f)
            if len(_meta_cache) > 4096:
                _meta_cache.pop(next(iter(_meta_cache)))
            _meta_cache[key] = meta
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        out.append({"file": key, **meta, "mtime": mt})
    out.sort(key=lambda x: -x["mtime"])
    return out


def _tail_events(path: Path, mtime: float, tail_bytes: int = 65536) -> list[tuple[str, str, float]]:
    """读文件尾，返回 [(type, payload.type, epoch)]（按时间正序）。

    按 (路径, mtime) 缓存：文件没增长不重读（高频调用的关键优化）。"""
    key = str(path)
    cached = _tail_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        import datetime as _dt

        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", errors="replace")
        events = []
        for line in tail.splitlines():
            line = line.strip()
            if not line or '"type"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type", "")
            pt = (d.get("payload") or {}).get("type", "")
            ts = d.get("timestamp")
            try:
                epoch = _dt.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            events.append((t, pt, epoch))
    except OSError:
        return []
    if len(_tail_cache) > 4096:
        _tail_cache.pop(next(iter(_tail_cache)))
    _tail_cache[key] = (mtime, events)
    return events


def codex_busy_state(rollout_file: str,
                     finish_silence_s: float = 90.0,
                     now: Optional[float] = None,
                     mtime: Optional[float] = None) -> Optional[dict]:
    """判定某 rollout 的忙闲（task_started/task_complete 轮次边界 + 静默阈值）。

    统一规则：静默 < finish_silence_s → BUSY（孤儿轮/完成态都靠时间兜底，
    教训：进程被杀会留下 task_started 无 complete 的孤儿轮，静默 6 天的都有）。
    """
    f = Path(rollout_file)
    now = time.time() if now is None else now
    try:
        mt = f.stat().st_mtime if mtime is None else mtime
    except OSError:
        return None
    events = _tail_events(f, mt)
    if not events:
        return None
    silence = max(0.0, now - mt)
    return {"busy": silence < finish_silence_s, "silence_s": round(silence, 1)}


def runtime_activity_age() -> Optional[float]:
    """Codex 桌面版运行时活动距今秒数（实时信号）。

    实测（任务运行中）：~/.codex/logs_2.sqlite-wal 与 state_5.sqlite-wal 秒级刷新；
    任务停止后静默。取多个运行时 WAL 的最新 mtime。
    """
    base = sessions_root().parent
    newest = None
    for name in ("logs_2.sqlite-wal", "state_5.sqlite-wal",
                 "logs_3.sqlite-wal", "state_6.sqlite-wal"):
        f = base / name
        try:
            mt = f.stat().st_mtime
            newest = mt if newest is None else max(newest, mt)
        except OSError:
            continue
    if newest is None:
        return None
    return max(0.0, time.time() - newest)


def read_sqlite_catalog() -> list[dict]:
    """读 ~/.codex/sqlite/codex-dev.db 的 local_thread_catalog（新版桌面/VSCode 会话）。

    WAL 复制读取；按 (db mtime, wal mtime) 缓存——库没变不复制（高频调用优化）。
    失败返回 []。字段：
    [{thread_id, title, cwd, source_kind, source_updated_at, git_branch}]
    """
    import shutil
    import sqlite3
    import tempfile

    src = sessions_root().parent / "sqlite" / "codex-dev.db"
    if not src.exists():
        return []
    try:
        key = (src.stat().st_mtime,
               src.with_name(src.name + "-wal").stat().st_mtime)
    except OSError:
        return []
    cached = _catalog_cache.get(key)
    if cached is not None:
        return cached
    tmp = Path(tempfile.gettempdir()) / f"arm_codex_ro_{os.getpid()}.db"
    try:
        shutil.copy2(src, tmp)
        for ext in ("-wal", "-shm"):
            try:
                shutil.copy2(src.with_name(src.name + ext), Path(str(tmp) + ext))
            except Exception:
                pass
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT thread_id, display_title, source_updated_at, cwd,"
            " source_kind, git_branch FROM local_thread_catalog"
            " ORDER BY source_updated_at DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            try:
                ts = float(r["source_updated_at"] or 0)
            except (TypeError, ValueError):
                ts = 0.0
            out.append({
                "thread_id": r["thread_id"],
                "title": r["display_title"],
                "cwd": r["cwd"],
                "source_kind": r["source_kind"],
                "git_branch": r["git_branch"],
                "source_updated_at": ts,
            })
        conn.close()
        _catalog_cache.clear()  # 单条缓存即可（键变化=库变了）
        _catalog_cache[key] = out
        return out
    except Exception:
        return []
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + ext).unlink(missing_ok=True)
            except Exception:
                pass


def read_sqlite_timeline() -> dict[str, dict]:
    """读取 Codex thread_timeline_ledger 的最新会话级状态。

    这是只读旁路：复制 db 与 WAL 后查询，避免占用 Codex 的写连接。返回值按
    thread_id 聚合为 ``{"state": ..., "ts": ..., "reason": ...}``；旧版数据库、
    WAL 不完整或单条 JSON 损坏时安全降级为空，不把全局 app-server 活动归因给会话。
    """
    import shutil
    import sqlite3
    import tempfile

    src = sessions_root().parent / "sqlite" / "codex-dev.db"
    if not src.exists():
        return {}
    try:
        key = (src.stat().st_mtime,
               src.with_name(src.name + "-wal").stat().st_mtime)
    except OSError:
        return {}
    cached = _timeline_cache.get(key)
    if cached is not None:
        return cached

    tmp = Path(tempfile.gettempdir()) / f"arm_codex_timeline_{os.getpid()}.db"
    try:
        shutil.copy2(src, tmp)
        for ext in ("-wal", "-shm"):
            try:
                shutil.copy2(src.with_name(src.name + ext), Path(str(tmp) + ext))
            except OSError:
                pass
        conn = sqlite3.connect(str(tmp))
        rows = conn.execute(
            "SELECT thread_id, sequence, payload_json FROM thread_timeline_ledger"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                Path(str(tmp) + ext).unlink(missing_ok=True)
            except OSError:
                pass

    out: dict[str, dict] = {}
    for thread_id, sequence, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        sid = thread_id
        session_sid = payload.get("sessionId")
        if not sid and not session_sid:
            continue
        typ = payload.get("type")
        status = payload.get("status")
        state = None
        reason = None
        if typ == "session-started":
            state, reason = "running", "session-started"
        elif typ == "voice-work-claimed":
            state, reason = "running", "claimed"
        elif typ == "session-ended":
            state, reason = "finished", payload.get("outcome") or "session-ended"
        elif typ == "voice-work-terminal" and status in ("completed", "interrupted"):
            state, reason = "finished", status
        if state is None:
            continue
        old = out.get(sid)
        if old is None or sequence >= old["sequence"]:
            item = {"state": state, "sequence": sequence, "reason": reason}
            out[sid] = item
            if session_sid:
                out[session_sid] = item
    if len(_timeline_cache) > 8:
        _timeline_cache.pop(next(iter(_timeline_cache)))
    _timeline_cache[key] = out
    return out
def busy_codex_sessions(finish_silence_s: float = 90.0,
                        max_age_s: float = 7 * 86400.0) -> list[dict]:
    """所有近期 Codex 会话（双源合并，busy 在前）。供引擎/UI。

    源 A（sqlite catalog/timeline）：终态优先；进行中事件须同时满足会话自身静默阈值
    源 B（rollout jsonl）：静默阈值（冷文件按 mtime 直接短路，不读尾）
    结果 2.5s TTL 缓存：引擎与 UI 的双调用共享一次真实扫描。
    调用方契约：只读返回的 dict（勿原地修改，缓存共享）。
    """
    now = time.time()
    key = (finish_silence_s, max_age_s)
    if (_result_cache["key"] == key
            and now - _result_cache["at"] < 2.5):
        return _result_cache["data"]

    out: dict[str, dict] = {}

    # 源 A：sqlite 目录（新版桌面/VSCode）
    # 仅使用会话自身 source_updated_at 与 timeline 终态。运行时 WAL 是全局
    # app-server 心跳，不能再用 idx==0 把它归因给最新会话。
    timeline = read_sqlite_timeline()
    rt_age = runtime_activity_age()
    cat_rows = read_sqlite_catalog()
    for r in cat_rows:
        ts = r.get("source_updated_at") or 0
        silence = max(0.0, now - ts)
        terminal = timeline.get(r["thread_id"])
        if terminal is not None and terminal.get("state") == "finished":
            busy = False
            activity_source = "timeline"
            finish_reason = terminal.get("reason")
        elif terminal is not None and terminal.get("state") == "running":
            busy = silence < finish_silence_s
            activity_source = "timeline+catalog"
            finish_reason = None if busy else "timeline 活跃但自身静默超时"
        else:
            busy = silence < finish_silence_s
            activity_source = "catalog"
            finish_reason = None if busy else "source_updated_at 静默超时"
        out[r["thread_id"]] = {
            "session_id": r["thread_id"],
            "title": r.get("title"),
            "cwd": r.get("cwd"),
            "source_kind": r.get("source_kind"),
            "git_branch": r.get("git_branch"),
            "busy": busy,
            "silence_s": round(silence, 1),
            "last_activity": ts,
            "activity_source": activity_source,
            "finish_reason": finish_reason,
            "runtime_age_s": round(rt_age, 1) if rt_age is not None else None,
            "src": "sqlite",
        }

    # 源 B：rollout jsonl（CLI/旧版）；sqlite 已有的补 originator（来源判据）
    rollout_by_sid = {}
    for info in scan_rollouts(max_age_s):
        rollout_by_sid[info["session_id"]] = info
        mtime_age = max(0.0, now - info["mtime"])
        existing = out.get(info["session_id"])
        if existing is not None:
            # 热 rollout 覆盖 catalog 误判（2026-09-29 实测回归：Codex 长任务执行中
            # catalog.source_updated_at 可十几分钟不刷新，"自身静默<90s"把真任务
            # 熄灯；rollout 是会话自身的直写日志、mtime 秒级实时——它在写=真在跑。
            # 宁可误保护：热 rollout 无条件覆盖 busy）
            if mtime_age < finish_silence_s:
                existing.update(busy=True, silence_s=round(mtime_age, 1),
                                last_activity=info["mtime"],
                                activity_source="rollout", finish_reason=None)
            continue
        if mtime_age >= finish_silence_s:
            # 冷文件短路：末次写入已超静默阈值，末条事件必然更早 → 直接非 busy
            st = {"busy": False, "silence_s": round(mtime_age, 1)}
        else:
            st = codex_busy_state(info["file"], finish_silence_s=finish_silence_s,
                                  now=now, mtime=info["mtime"])
            if st is None:
                continue
        out[info["session_id"]] = {
            "session_id": info["session_id"],
            "originator": info["originator"],
            "cwd": info["cwd"],
            "last_activity": info["mtime"],
            "activity_source": "rollout",
            **st,
            "src": "rollout",
        }

    # origin 判定（四类体系）：desktop / cli
    #   sqlite 源：source_kind=chatgpt → desktop（ChatGPT 端创建）
    #   rollout 源：originator=codex_work_desktop → desktop
    for sid, item in out.items():
        if item.get("src") == "sqlite":
            kind = item.get("source_kind")
            ro = rollout_by_sid.get(sid)
            ro_originator = (ro or {}).get("originator")
            item["origin"] = "desktop" if (
                kind == "chatgpt" or ro_originator == "codex_work_desktop") else "cli"
        else:
            item["origin"] = "desktop" if item.get("originator") == "codex_work_desktop" else "cli"

    merged = list(out.values())
    merged = [x for x in merged if x["busy"]] + [x for x in merged if not x["busy"]]
    _result_cache.update(key=key, at=now, data=merged)
    return merged
