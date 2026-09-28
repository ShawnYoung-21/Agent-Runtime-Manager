"""SQLite 状态/事件存储（T2）。

设计要点（对应 Architecture §core）：
- 引擎进程与 hook-ingress 进程是不同进程，都可能写库 → 开启 WAL 降低锁竞争。
- hook-ingress 是高频短进程，写入必须毫秒级、失败不抛（绝不阻塞 Claude）。
- 读侧（status/engine）用独立连接，跨进程通过 SQLite 共享状态。

表：
  agent_events   原始生命周期事件流水（hook / 进程探测产生）
  agent_state    每个 agent 会话的最新语义状态（upsert）
  protection     单行保护状态（DISARMED/ARMED/PROTECTING）
  env_events     环境事件流水（电源/待机/网络）
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    source        TEXT NOT NULL,           -- 'claude_hook' | 'codex_proc' | ...
    event         TEXT NOT NULL,           -- 原始事件名，如 UserPromptSubmit
    semantic      TEXT,                    -- 语义状态：STARTED/BUSY/IDLE/STOPPED
    session_id    TEXT,
    cwd           TEXT,
    payload_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events(ts);

CREATE TABLE IF NOT EXISTS agent_state (
    session_id    TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    state         TEXT NOT NULL,           -- RUNNING/FINISHED/STOPPED
    substate      TEXT,                    -- busy/idle
    cwd           TEXT,
    updated_ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS protection (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    state         TEXT NOT NULL,           -- DISARMED/ARMED/PROTECTING
    updated_ts    REAL NOT NULL,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS daemon_heartbeat (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    ts   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS env_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,           -- power/standby/network
    detail        TEXT,
    payload_json  TEXT
);
"""


class Store:
    """对 ~/.arm/arm.db 的薄封装。每个方法自带连接，进程间安全。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else _default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ---- agent events ----
    def record_agent_event(
        self,
        *,
        source: str,
        event: str,
        semantic: Optional[str] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        ts: Optional[float] = None,
    ) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO agent_events"
                " (ts, source, event, semantic, session_id, cwd, payload_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    source,
                    event,
                    semantic,
                    session_id,
                    cwd,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
            conn.commit()

    def upsert_agent_state(
        self,
        *,
        session_id: str,
        source: str,
        state: str,
        substate: Optional[str] = None,
        cwd: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO agent_state (session_id, source, state, substate, cwd, updated_ts)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   source=excluded.source, state=excluded.state,"
                "   substate=excluded.substate,"
                # cwd 为空时保留旧值（Stop 等事件不带 cwd，避免覆盖丢上下文）
                "   cwd=COALESCE(excluded.cwd, agent_state.cwd),"
                "   updated_ts=excluded.updated_ts",
                (session_id, source, state, substate, cwd, ts if ts is not None else time.time()),
            )
            conn.commit()

    def get_agent_states(self) -> list[sqlite3.Row]:
        with contextlib.closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT session_id, source, state, substate, cwd, updated_ts"
                " FROM agent_state ORDER BY updated_ts DESC"
            )
            return cur.fetchall()

    def recent_agent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with contextlib.closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT ts, source, event, semantic, session_id"
                " FROM agent_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    # ---- protection ----
    def set_protection(self, state: str, reason: Optional[str] = None) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO protection (id, state, updated_ts, reason) VALUES (1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   state=excluded.state, updated_ts=excluded.updated_ts, reason=excluded.reason",
                (state, time.time(), reason),
            )
            conn.commit()

    def get_protection(self) -> Optional[sqlite3.Row]:
        with contextlib.closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT state, updated_ts, reason FROM protection WHERE id=1")
            return cur.fetchone()

    def get_effective_protection(self, heartbeat_timeout_s: float = 30.0) -> dict:
        """读取"有效"保护状态，处理 daemon 硬杀导致的状态滞留。

        daemon 每次 tick 都会刷新 updated_ts（心跳）。若 updated_ts 距今超过
        heartbeat_timeout_s，说明 daemon 已死（硬杀/崩溃时来不及 release），
        其写的 PROTECTING/ARMED 视为失效 → 归一为 DISARMED（stale=True）。

        返回 {state, updated_ts, reason, stale}。电源安全由 OS 保证
        （进程死即清除执行状态），这里只修正"显示给用户的逻辑状态"。
        """
        row = self.get_protection()
        if row is None:
            return {"state": "DISARMED", "updated_ts": None, "reason": None, "stale": False}
        state, ts, reason = row["state"], row["updated_ts"], row["reason"]
        stale = (
            state in ("PROTECTING", "ARMED")
            and ts is not None
            and (time.time() - ts) > heartbeat_timeout_s
        )
        if stale:
            return {"state": "DISARMED", "updated_ts": ts,
                    "reason": f"daemon 心跳超时，已失效(原:{state})", "stale": True}
        return {"state": state, "updated_ts": ts, "reason": reason, "stale": False}

    # ---- daemon heartbeat ----
    def beat(self) -> None:
        """daemon 每次 tick 调用：刷新独立心跳（与 protection 状态解耦）。"""
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO daemon_heartbeat (id, ts) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET ts=excluded.ts",
                (time.time(),),
            )
            conn.commit()

    def heartbeat_age(self) -> Optional[float]:
        """距 daemon 上次心跳的秒数；从未跳过返回 None。"""
        with contextlib.closing(self._connect()) as conn:
            cur = conn.execute("SELECT ts FROM daemon_heartbeat WHERE id=1")
            row = cur.fetchone()
            return (time.time() - row[0]) if row else None

    # ---- env events ----
    def record_env_event(
        self, *, kind: str, detail: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None, ts: Optional[float] = None,
    ) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO env_events (ts, kind, detail, payload_json) VALUES (?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    kind,
                    detail,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
            conn.commit()

    # ---- 历史清理 ----
    def prune(self, events_keep_s: float = 30 * 86400.0,
              terminal_keep_s: float = 14 * 86400.0) -> dict[str, int]:
        """清理历史数据，防止 arm.db 无界增长（引擎每 6h 调一次）。

        agent_events/env_events 留 30 天；agent_state 终态（STOPPED/FINISHED）
        留 14 天；RUNNING 永不清理。返回各类删除行数（日志用）。
        """
        cutoff = time.time() - events_keep_s
        terminal_cutoff = time.time() - terminal_keep_s
        with contextlib.closing(self._connect()) as conn:
            c1 = conn.execute(
                "DELETE FROM agent_events WHERE ts < ?", (cutoff,)).rowcount
            c2 = conn.execute(
                "DELETE FROM env_events WHERE ts < ?", (cutoff,)).rowcount
            c3 = conn.execute(
                "DELETE FROM agent_state WHERE state IN ('STOPPED','FINISHED')"
                " AND updated_ts < ?", (terminal_cutoff,)).rowcount
            conn.commit()
        return {"events": c1, "env_events": c2, "terminal_sessions": c3}


def _default_db_path() -> Path:
    from arm.core import paths

    return paths.db_path()


@contextmanager
def open_store(path: Optional[Path] = None) -> Iterator[Store]:
    """便捷上下文。Store 本身无长连接，这里主要为语义清晰。"""
    yield Store(path)
