"""单实例锁（T2）：防止多个 engine 同时抢占电源控制。

实现演进：
- v1 用 msvcrt.locking 文件锁 —— 但文件锁按"文件描述符"隔离，两个进程各自 open()
  得到不同 fd，互不互斥，锁形同虚设（已废弃）。
- v2（当前）Windows 用**命名互斥体 Named Mutex**（CreateMutex + GetLastError=
  ERROR_ALREADY_EXISTS）：系统全局、跨进程可靠，进程退出由 OS 自动释放。
  非 Windows 平台退回 fcntl 文件锁（开发/CI 用）。

用法：
    try:
        with single_instance("engine"):
            ...   # 独家运行
    except SingleInstanceError:
        ...       # 已有实例
"""

from __future__ import annotations

import contextlib
import sys
from typing import Iterator


class SingleInstanceError(RuntimeError):
    """已有实例持有锁。"""


if sys.platform == "win32":
    import ctypes

    # 陷阱（2026-09-28 双引擎风暴根源）：windll 模式下 ctypes 内部调用会冲掉线程
    # lasterror，GetLastError() 读到 0 → 第二实例误判"无人持锁"双双运行。
    # 必须 use_last_error=True + ctypes.get_last_error()（ctypes 在调用前后
    # 保存/恢复错误码，跨进程实测：held 锁正确读到 183）。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ERROR_ALREADY_EXISTS = 183

    @contextlib.contextmanager
    def single_instance(name: str = "engine", lock_file=None) -> Iterator[bool]:
        """Windows：命名互斥体实现的全局单实例。"""
        mutex_name = f"Local\\arm-{name}-mutex"
        handle = _kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise SingleInstanceError(f"无法创建互斥体: {mutex_name}")
        try:
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                raise SingleInstanceError(f"已有实例持有锁: {mutex_name}")
            yield True
        finally:
            _kernel32.ReleaseMutex(handle)
            _kernel32.CloseHandle(handle)

else:
    import fcntl
    import os
    from pathlib import Path

    @contextlib.contextmanager
    def single_instance(name: str = "engine", lock_file=None) -> Iterator[bool]:
        """POSIX：fcntl 文件锁（按文件 inode 互斥，跨进程有效）。"""
        if lock_file is None:
            from arm.core import paths

            lock_file = paths.data_dir() / f"{name}.lock"
        lock_file = Path(lock_file)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_file, "a+b")
        try:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SingleInstanceError(f"已有实例持有锁: {lock_file}") from exc
            f.seek(0)
            f.truncate()
            f.write(f"pid={os.getpid()} name={name}\n".encode())
            f.flush()
            yield True
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()
