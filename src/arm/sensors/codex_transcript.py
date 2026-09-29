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
    """解析 rollout 首行 session_meta（头部不可变，结果进 _meta_cache）。

    ``_pending`` 标记：文件刚创建、session_meta 行还没落盘时置位——
    调用方不得缓存该结果（2026-09-29 竞态修复：半行 JSON 解析失败若被
    永久缓存，会话归属将从此错乱）。"""
    info = {"session_id": _session_id_of(f.name),
            "originator": None, "cwd": None}
    found = False
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
                    found = True
                    break
                if d.get("type") == "turn_context":
                    break  # meta 不会出现在 turn_context 之后
    except OSError:
        pass
    if not found:
        info["_pending"] = True
    return info


def scan_rollouts(max_age_s: float = 7 * 86400.0) -> list[dict]:
    """扫描 rollout 文件，解析首行 session_meta（头部缓存，文件不重读）。

    返回 [{file, session_id, originator, cwd, mtime}]（mtime 新→旧）。
    """
    out = []
    now = time.time()
    for f in _iter_rollouts(max_age_s):
        key = str(f)
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        meta = _meta_cache.get(key)
        if meta is None:
            meta = _read_meta(f)
            if meta.pop("_pending", False) and now - mt < 60.0:
                # 头部未写全（文件刚创建）：不缓存，下次扫描重读
                out.append({"file": key, **meta, "mtime": mt})
                continue
            meta.pop("_pending", None)
            if len(_meta_cache) > 4096:
                _meta_cache.pop(next(iter(_meta_cache)))
            _meta_cache[key] = meta
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


OPEN_TURN_CAP_S = 1800.0  # 回合打开（执行中/等待批准）的最长活跃窗口，防孤儿轮泄漏


def codex_busy_state(rollout_file: str,
                     finish_silence_s: float = 90.0,
                     now: Optional[float] = None,
                     mtime: Optional[float] = None,
                     open_turn_cap_s: float = OPEN_TURN_CAP_S) -> Optional[dict]:
    """判定某 rollout 的忙闲（turn 边界 + 静默阈值，2026-09-29 升级）。

    实测依据：完成任务末尾有 event_msg/task_complete；执行中/等待批准的
    回合只有 task_started 无 complete（请求批准期间零写入，纯静默判定会
    把"等你批"误判成结束）。规则：
      - 最后边界是 task_complete → 显式完成，立即非 busy（比静默等待更快释放）
      - 回合打开（started 无 complete）→ busy，窗口 open_turn_cap_s 内有效
        （等待批准也算任务没完；孤儿轮被窗口兜底，不会永久亮灯）
      - 无边界标记的旧格式 → 退回纯静默规则
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

    def _last_boundary(pt: str) -> Optional[float]:
        ts = None
        for _t, payload_t, epoch in events:
            if payload_t == pt and (ts is None or epoch > ts):
                ts = epoch
        return ts

    started, complete = _last_boundary("task_started"), _last_boundary("task_complete")
    if complete is not None and (started is None or complete >= started):
        return {"busy": False, "silence_s": round(silence, 1), "turn": "complete"}
    if started is not None:
        return {"busy": silence < open_turn_cap_s, "silence_s": round(silence, 1),
                "turn": "open"}
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


_turn_cache: dict[str, object] = {"at": 0.0, "data": None}


def read_runtime_turn_activity(max_age_s: float = 180.0) -> dict[str, float]:
    """读 ~/.codex/logs_2.sqlite 最近的 turn 执行跨度 → {thread_id: 最近活动 epoch}。

    第三路独立证据（2026-09-29）：任务执行时 logs_2 秒级流式出现
    ``session_task.turn`` 跨度行（带 thread_id=uuid），空闲即消失——
    与 rollout 文件、catalog 心跳互不依赖，防任一单点失效。
    只读连接（WAL 并发读安全，logs_2 大，不做复制）；失败返回上次缓存或 {}。
    """
    import re
    import sqlite3

    p = sessions_root().parent / "logs_2.sqlite"
    if not p.exists():
        return {}
    now = time.time()
    cached = _turn_cache.get("data")
    if cached is not None and now - float(_turn_cache.get("at", 0.0)) < 2.0:
        return dict(cached)  # type: ignore[arg-type]
    out: dict[str, float] = {}
    pat = re.compile(r"thread_id=([0-9a-fA-F-]{36})")
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True,
                               timeout=1.0)
        rows = conn.execute(
            "SELECT ts, feedback_log_body FROM logs ORDER BY id DESC LIMIT 400"
        ).fetchall()
        conn.close()
        for ts, body in rows:
            if ts < now - max_age_s:
                break
            if not body or "session_task.turn" not in body:
                continue
            m = pat.search(body)
            if m:
                tid = m.group(1).lower()
                if tid not in out or ts > out[tid]:
                    out[tid] = float(ts)
    except Exception:
        return dict(cached) if isinstance(cached, dict) else {}
    _turn_cache.update(at=now, data=out)
    return out


def read_sqlite_catalog() -> list[dict]:
    """读 ~/.codex/sqlite/codex-dev.db 的 local_thread_catalog（新版桌面/VSCode 会话）。

    WAL 复制读取；按 (db mtime, wal mtime) 缓存——库没变不复制（高频调用优化）。
    失败返回 []。字段：
    [{thread_id, title, cwd, source_kind, source_updated_at, git_branch}]
    """
    import shutil
    import sqlite3
    import tempfile
    import threading

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
    # 临时名含线程 id：单进程双线程（引擎+UI）并发失效时会同时走到这里，
    # 同名临时文件互相踩踏会让传感器瞬间致盲（2026-09-29 并发修复）
    tmp = (Path(tempfile.gettempdir())
           / f"arm_codex_ro_{os.getpid()}_{threading.get_ident()}.db")
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
    import threading

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

    # 同 read_sqlite_catalog：临时名含线程 id，防双线程踩踏
    tmp = (Path(tempfile.gettempdir())
           / f"arm_codex_timeline_{os.getpid()}_{threading.get_ident()}.db")
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
                        max_age_s: float = 7 * 86400.0,
                        open_turn_cap_s: float = OPEN_TURN_CAP_S) -> list[dict]:
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
    # 判定优先级（2026-09-29 定版）：热 rollout > catalog 自身静默 > timeline 终态
    # > runtime turn 跨度（第三路独立证据，防 rollout/catalog 单点失效）。
    # 全局 WAL/常驻进程不得归因给具体会话。
    timeline = read_sqlite_timeline()
    turns = read_runtime_turn_activity()
    rt_age = runtime_activity_age()
    cat_rows = read_sqlite_catalog()
    for r in cat_rows:
        ts = r.get("source_updated_at") or 0
        silence = max(0.0, now - ts)
        tid = r["thread_id"]
        terminal = timeline.get(tid)
        if terminal is not None and terminal.get("state") == "finished":
            busy = False
            activity_source = "timeline"
            finish_reason = terminal.get("reason")
        else:
            busy = silence < finish_silence_s
            activity_source = "catalog"
            finish_reason = None if busy else "source_updated_at 静默超时"
        turn_ts = turns.get(tid)
        if turn_ts is not None and (now - turn_ts) < finish_silence_s:
            # turn 跨度在窗口内 = 任务此刻真在执行（覆盖 timeline 终态与 catalog 判闲：
            # 终态后新回合、catalog 轮次边界心跳失灵，都以实时跨度为准——宁误保护）
            busy = True
            silence = min(silence, now - turn_ts)
            activity_source = "runtime_turn"
            finish_reason = None
        out[tid] = {
            "session_id": tid,
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
    # turn 跨度里出现、catalog 却没有的线程：补最小 busy 条目（宁可误保护）
    for tid, t_ts in turns.items():
        if tid in out or (now - t_ts) >= finish_silence_s:
            continue
        out[tid] = {
            "session_id": tid, "title": None, "cwd": None,
            "source_kind": None, "git_branch": None,
            "busy": True, "silence_s": round(now - t_ts, 1),
            "last_activity": t_ts, "activity_source": "runtime_turn",
            "finish_reason": None,
            "runtime_age_s": round(rt_age, 1) if rt_age is not None else None,
            "src": "sqlite",
        }

    # 源 B：rollout jsonl（CLI/旧版）；sqlite 已有的补 originator（来源判据）
    rollout_by_sid = {}
    for info in scan_rollouts(max_age_s):
        rollout_by_sid[info["session_id"]] = info
        mtime_age = max(0.0, now - info["mtime"])
        # 判定：mtime<90 热写即活跃；<30min 交给 turn 边界（等待批准的回合
        # 零写入也要亮灯）；完成态显式熄灯；更冷直接短路
        st: Optional[dict]
        if mtime_age < open_turn_cap_s:
            st = codex_busy_state(info["file"], finish_silence_s=finish_silence_s,
                                  now=now, mtime=info["mtime"],
                                  open_turn_cap_s=open_turn_cap_s)
        else:
            st = {"busy": False, "silence_s": round(mtime_age, 1)}
        if st is None:  # 文件读不到：热写兜底为活跃，冷文件视为结束
            st = {"busy": mtime_age < finish_silence_s,
                  "silence_s": round(mtime_age, 1)}
        turn = st.pop("turn", None)  # 先取键再分支，勿提前 pop（complete 判断要用）
        if turn == "open":
            st["activity_source"] = "turn_open"
        existing = out.get(info["session_id"])
        if existing is not None:
            # 完成事件是强证据：task_complete 立即熄灯（catalog 恰在完成时
            # 刷新带来的 90s 假忙让位）；但不得覆盖更新的 runtime_turn 信号
            if turn == "complete":
                if existing["busy"] and existing.get("activity_source") != "runtime_turn":
                    existing.update(busy=False, finish_reason="task_complete")
                continue
            # rollout 忙（含等待批准的打开回合）覆盖 catalog 判闲——宁可误保护；
            # 2026-09-29 实测：长任务 catalog 心跳可十几分钟不刷，等待批准零写入
            if st["busy"] and not existing["busy"]:
                existing.update(busy=True, silence_s=st["silence_s"],
                                last_activity=info["mtime"],
                                activity_source=st.get("activity_source", "rollout"),
                                finish_reason=None)
            elif st["busy"]:
                existing["silence_s"] = min(existing["silence_s"], st["silence_s"])
            continue
        out[info["session_id"]] = {
            "session_id": info["session_id"],
            "originator": info["originator"],
            "cwd": info["cwd"],
            "last_activity": info["mtime"],
            "activity_source": st.get("activity_source", "rollout"),
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
