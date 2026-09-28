"""arm 日志（L4 可观测：daemon 出问题可回溯）。

轻量方案：标准 logging + RotatingFileHandler（~/.arm/arm.log，5MB×3 轮转）。
daemon 每拍的关键事件（状态变迁/错误/对账结果）写这里。
ARM_DEBUG=1 时同时输出到 stderr。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def get_logger() -> logging.Logger:
    """进程级单例 logger。"""
    from arm.core import paths

    logger = logging.getLogger("arm")
    if logger.handlers:  # 已初始化
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%m-%d %H:%M:%S")

    try:
        log_dir = paths.data_dir()
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "arm.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # 文件不可用时静默降级（不阻塞主功能）

    if os.environ.get("ARM_DEBUG"):
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger
