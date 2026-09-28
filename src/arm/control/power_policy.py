"""电源策略组合拳（T15）：保活确定性升级。

参照 Always-Up（github.com/sudoatif/Always-Up，MIT）的实践：
合盖动作与执行状态请求是两条独立策略，SetThreadExecutionState 只覆盖后者。
合盖后系统走"无人值守空闲"路径（UNATTENDSLEEP），必须临时把它设为"永不"，
保活才是确定性的，而不是依赖"开盖前的窗口期"。

与竞品的差异（我们的优势）：
- 生命周期驱动：进入保护 Apply、释放 Restore，全自动，不需要人按开关。
- 崩溃安全：原值快照落盘 ~/.arm/power_backup.json——daemon 硬杀后，
  下次启动检测到"有快照但未恢复"会自动还原，绝不把用户的电源配置改丢。
- 只动需要的：UNATTENDSLEEP / STANDBYIDLE / HIBERNATEIDLE / VIDEOIDLE 四项。
  不碰 LID 动作（S0 设备隐藏该设置，收益不明）和 VIDEOCONLOCK（外接屏场景）。

技术路线（学它的教训）：直接 powrprof.dll 的 PowerRead/WriteAC|DCValueIndex，
不解析 powercfg 输出——隐藏设置 powercfg 看不见，且文案本地化、GUID 全球一致。
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from typing import Iterator, Optional

if sys.platform != "win32":
    _powrprof = None
else:
    import ctypes
    from ctypes import wintypes

    try:
        _powrprof = ctypes.windll.powrprof
    except Exception:
        _powrprof = None

# 电源设置 GUID（全球一致，与本地化文案无关）
_SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
_SUB_VIDEO = "7516b95f-f776-4464-8c53-06167f40cc99"
_SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
_POWER_BUTTON = "7648efa3-dd9c-4e3e-b566-50f929386280"  # 电源键动作

# 我们要盯的电源设置：(名称, 子组GUID, 设置GUID)
POLICY_SETTINGS_MAP: dict[tuple[str, str], str] = {}  # (sub, gid) -> name（测试/查表用）

POLICY_SETTINGS = [
    ("unattend_sleep", _SUB_SLEEP, "7bc4a2f9-d8fc-4469-b07b-33eb785aaca0"),  # 无人值守睡眠
    ("standby_idle", _SUB_SLEEP, "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"),    # 睡眠超时
    ("hibernate_idle", _SUB_SLEEP, "9d7815a6-7ee4-497e-8888-515a05f02364"),  # 休眠超时
    ("video_idle", _SUB_VIDEO, "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e"),      # 关显示超时
]

# 实际钉"永不"的三项（2026-09-28 复审调整）：关屏不影响 away mode 保活
# （熄屏≠睡眠，UNATTENDSLEEP/STANDBYIDLE/HIBERNATEIDLE 钉死即够），
# 不钉 video_idle 让屏幕按用户自己的设置正常熄灭——省电且零风险。
# 注意：电源键动作（本机=睡眠）是用户主动操作，不钉也不该钉——
# 保护期间按电源键系统会睡眠，agent 暂停（手册已知边界）。
PIN_SETTINGS = [t for t in POLICY_SETTINGS if t[0] != "video_idle"]

for _n, _s, _g in POLICY_SETTINGS:
    POLICY_SETTINGS_MAP[(_s, _g)] = _n

_NEVER = 0  # 0 = 从不


class _GUID(ctypes.Structure):
    """Windows GUID 结构体（powrprof 的 GUID 参数按此结构 byref 传）。"""

    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    from uuid import UUID

    g = _GUID()
    ctypes.memmove(ctypes.byref(g), UUID(s).bytes_le, 16)
    return g


def _read_index(sub: str, gid: str) -> Optional[tuple[int, int]]:
    """读当前方案下某设置的 (AC, DC) 索引。失败返回 None。"""
    if _powrprof is None:
        return None
    try:
        import ctypes

        scheme = _active_scheme()
        if scheme is None:
            return None
        subg = _guid(sub)
        setg = _guid(gid)
        ac = wintypes.DWORD()
        dc = wintypes.DWORD()
        ok1 = _powrprof.PowerReadACValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(subg), ctypes.byref(setg),
            ctypes.byref(ac))
        ok2 = _powrprof.PowerReadDCValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(subg), ctypes.byref(setg),
            ctypes.byref(dc))
        if ok1 != 0 or ok2 != 0:
            return None
        return (ac.value, dc.value)
    except Exception:
        return None


def _write_index(sub: str, gid: str, ac: int, dc: int) -> bool:
    if _powrprof is None:
        return False
    try:
        scheme = _active_scheme()
        if scheme is None:
            return False
        subg = _guid(sub)
        setg = _guid(gid)
        ok1 = _powrprof.PowerWriteACValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(subg), ctypes.byref(setg),
            wintypes.DWORD(ac))
        ok2 = _powrprof.PowerWriteDCValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(subg), ctypes.byref(setg),
            wintypes.DWORD(dc))
        if ok1 != 0 or ok2 != 0:
            return False
        # 写入在方案重新激活后才生效（Always-Up 同款做法）
        _powrprof.PowerSetActiveScheme(None, ctypes.byref(scheme))
        return True
    except Exception:
        return False


def _active_scheme() -> Optional[_GUID]:
    import ctypes

    ptr = ctypes.c_void_p()
    if _powrprof.PowerGetActiveScheme(None, ctypes.byref(ptr)) != 0 or not ptr:
        return None
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), ctypes.string_at(ptr, 16), 16)
    ctypes.windll.kernel32.LocalFree(ptr)
    return g


def read_current() -> dict[str, Optional[tuple[int, int]]]:
    """当前四项的 (AC,DC) 值（doctor/UI 展示用）。"""
    return {name: _read_index(sub, gid) for name, sub, gid in POLICY_SETTINGS}


# ---- 快照与恢复 ----

@contextlib.contextmanager
def _power_lock(timeout_s: float = 5.0) -> Iterator[None]:
    """跨进程序列化"读值→写快照→钉住"与"读快照→写回→删快照"两个序列。

    2026-09-28 事故：双引擎（或引擎+CLI protect）并发时，A 的 restore+删快照
    与 B 的 fresh snapshot 交错，把已钉住的 0 当原值存进快照（真原值丢失）。
    复用 single_instance 的命名互斥体（同款命名空间），超时退化为不加锁继续
    （保护优先，宁钉勿滞）。注意不可嵌套使用（存在性检测会自我冲突）。
    """
    from arm.core.single_instance import SingleInstanceError, single_instance

    deadline = time.time() + timeout_s
    while True:
        try:
            with single_instance("power"):
                yield
                return
        except SingleInstanceError:
            if time.time() >= deadline:
                yield
                return
            time.sleep(0.05)


def _backup_path():
    from arm.core import paths

    return paths.data_dir() / "power_backup.json"


def _load_backup() -> Optional[dict]:
    try:
        return json.loads(_backup_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_backup(data: dict) -> None:
    try:
        _backup_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def apply_pinned() -> dict:
    """进入保护：快照原值并钉住睡眠三项为"永不"。返回 {pinned: bool, ...}。

    幂等：已有未恢复的快照时不重复快照（避免覆盖真正的原值）。
    """
    with _power_lock():
        existing = _load_backup()
        if existing and existing.get("values"):
            # 已钉住未恢复——不重复快照，直接确认钉住
            _pin_all()
            return {"pinned": True, "already": True}

        snap = {}
        for name, sub, gid in PIN_SETTINGS:
            v = _read_index(sub, gid)
            if v is not None:
                snap[name] = {"sub": sub, "gid": gid, "ac": v[0], "dc": v[1]}
        if not snap:
            return {"pinned": False, "error": "无法读取电源设置（权限或非 Windows）"}

        _save_backup({"values": snap, "ts": time.time()})
        ok = _pin_all()
        return {"pinned": ok, "snapshot_count": len(snap)}


def _pin_all() -> bool:
    ok = True
    for name, sub, gid in PIN_SETTINGS:
        if not _write_index(sub, gid, _NEVER, _NEVER):
            ok = False
    return ok


def restore_original() -> dict:
    """释放保护：从快照还原原值。返回 {restored: bool, ...}。

    无快照 = 本来就没钉过，视为成功（幂等）。
    """
    with _power_lock():
        data = _load_backup()
        if not data or not data.get("values"):
            return {"restored": True, "note": "无快照，无需恢复"}

        ok = True
        for name, v in data["values"].items():
            if not _write_index(v["sub"], v["gid"], v["ac"], v["dc"]):
                ok = False
        if ok:
            try:
                _backup_path().unlink(missing_ok=True)
            except Exception:
                pass
        return {"restored": ok}


def recover_if_stranded() -> dict:
    """崩溃自愈：启动时调用。发现"有快照未恢复"（daemon 曾硬杀）→ 自动还原。

    这是比竞品强的地方：它的备份在内存，进程死了原值就丢了；我们的在磁盘。
    注意：只管保护钉组；电源键快照是 app 生命周期，由 app.py 启动时
    "先 restore_power_button 再 pin_power_button"自愈（顺序不能反）。
    """
    data = _load_backup()
    if not data or not data.get("values"):
        return {"recovered": False, "note": "无滞留快照"}
    r = restore_original()
    return {"recovered": r.get("restored", False)}


# ---- 电源键钉组（app 生命周期：在=防误按，退=还原；2026-09-28 二态模型） ----
# 与保护钉组分离：保护释放时 app 还活着，电源键必须保持钉住；
# 单独快照文件，崩溃后同样由 recover_if_stranded 自愈。

def _button_backup_path():
    from arm.core import paths

    return paths.data_dir() / "power_button_backup.json"


def _load_button_backup() -> Optional[dict]:
    try:
        return json.loads(_button_backup_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def pin_power_button() -> dict:
    """app 启动：快照电源键动作原值并临时钉成"不动作"（0）。

    幂等：已有未恢复快照时不重复快照。目的=防误按（单击睡眠会冻结 agent）；
    长按 4 秒强关是硬件级行为，不受影响。
    """
    with _power_lock():
        existing = _load_button_backup()
        if existing and existing.get("values"):
            ok = _write_index(_SUB_BUTTONS, _POWER_BUTTON, _NEVER, _NEVER)
            return {"pinned": ok, "already": True}
        v = _read_index(_SUB_BUTTONS, _POWER_BUTTON)
        if v is None:
            return {"pinned": False, "error": "无法读取电源键动作"}
        try:
            _button_backup_path().write_text(
                json.dumps({"values": {"power_button": {"sub": _SUB_BUTTONS,
                             "gid": _POWER_BUTTON, "ac": v[0], "dc": v[1]}},
                            "ts": time.time()}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            return {"pinned": False, "error": "快照落盘失败"}
        ok = _write_index(_SUB_BUTTONS, _POWER_BUTTON, _NEVER, _NEVER)
        return {"pinned": ok, "snapshot": v}


def restore_power_button() -> dict:
    """app 退出：还原电源键动作原值。无快照 = 没钉过，幂等成功。"""
    with _power_lock():
        data = _load_button_backup()
        if not data or not data.get("values"):
            return {"restored": True, "note": "无快照，无需恢复"}
        v = data["values"]["power_button"]
        ok = _write_index(v["sub"], v["gid"], v["ac"], v["dc"])
        if ok:
            try:
                _button_backup_path().unlink(missing_ok=True)
            except Exception:
                pass
        return {"restored": ok}
