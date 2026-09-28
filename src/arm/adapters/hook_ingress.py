"""接收 Claude Code hook 的 stdin JSON，落库为 AgentEvent（T5 接入 store）。

设计约束（Architecture §4.3）：必须毫秒级返回，绝不阻塞 Claude 主流程。
- 任何异常都静默吞掉、退出 0，不抛回给 Claude。
- 只依赖标准库 + core.store（其内部已用 WAL 降低多进程写竞争）。

事件→语义映射（依据 Architecture §4.3 已确认语义）：
  SessionStart     → STARTED  (RUNNING/busy)
  UserPromptSubmit → BUSY     (RUNNING/busy)
  Stop             → IDLE     (RUNNING/idle)  —— 一轮响应结束，等待输入
  SessionEnd       → STOPPED
  其它（Notification 等）→ 记录事件，但不改 agent_state
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from arm.core.store import Store

# hook 事件名 → (semantic, state, substate)；None 表示只记事件不动状态
_EVENT_MAP: dict[str, tuple[Optional[str], Optional[str], Optional[str]]] = {
    "SessionStart": ("STARTED", "RUNNING", "busy"),
    "UserPromptSubmit": ("BUSY", "RUNNING", "busy"),
    "Stop": ("IDLE", "RUNNING", "idle"),
    "SessionEnd": ("STOPPED", "STOPPED", None),
}


def run(event: str, store: Optional["Store"] = None, selftest: bool = False,
        allow_real: bool = False) -> None:
    """从 stdin 读 hook JSON，附加事件名与时间戳，落库。永不抛异常。

    store       可注入（测试用隔离库）；
    selftest    True 时写临时库（演示/自检），不碰真实 ~/.arm/arm.db；
    allow_real  True 才允许落真实 ~/.arm/arm.db —— 仅生产 hook 调用（CLI 入口）传 True。
                默认 False：防止测试/误调把假数据写进真实库（测试污染生产的修复）。
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        payload: dict[str, Any] = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except json.JSONDecodeError:
        payload = {"_raw": raw}

    # 以 stdin 里的 hook_event_name 为准（--event 仅作冗余/校验）
    event_name = payload.get("hook_event_name") or event

    record = {
        "event": event_name,
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "payload": payload,
    }
    try:
        if selftest:
            import tempfile
            from pathlib import Path

            from arm.core.store import Store as _Store

            with tempfile.TemporaryDirectory(prefix="arm-selftest-") as td:
                mem = _Store(Path(td) / "selftest.db")
                _persist(record, mem)
            print(f"[selftest] 已接收并解析 hook 事件：{event_name}"
                  f" session={record.get('session_id')}（未写入真实库）")
        elif store is not None:
            _persist(record, store)          # 注入了隔离库（测试）
        elif allow_real:
            _persist(record, None)           # 显式允许 → 真实库（生产 hook）
        else:
            # 既没注入 store、又未 allow_real：拒写真实库，只解析不落库
            print(f"[skip] 未授权写真实库，仅解析事件：{event_name}"
                  f" session={record.get('session_id')}")
    except Exception:
        # 存储失败也绝不影响 Claude；详细错误后续接 core.logging
        pass


def _persist(record: dict, store: Optional["Store"] = None) -> None:
    if store is None:
        from arm.core.store import Store as _Store

        store = _Store()

    event_name: str = record["event"]
    semantic, state, substate = _EVENT_MAP.get(event_name, (None, None, None))

    store.record_agent_event(
        source="claude_hook",
        event=event_name,
        semantic=semantic,
        session_id=record.get("session_id"),
        cwd=record.get("cwd"),
        payload=record.get("payload"),
    )
    # 仅当事件有状态语义、且能定位会话时，更新最新状态
    if state is not None and record.get("session_id"):
        store.upsert_agent_state(
            session_id=record["session_id"],
            source="claude_hook",
            state=state,
            substate=substate,
            cwd=record.get("cwd"),
        )
