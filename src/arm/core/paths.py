"""统一路径：arm 的运行时数据都放这里。

约定 `~/.arm/` 为数据目录：
  arm.db          SQLite 事件/状态库
  arm.lock        单实例锁
  arm.log         日志
  config.toml     配置
跨平台：用 Path.home()，Windows 下即 C:\\Users\\<user>\\.arm\\
"""

from __future__ import annotations

from pathlib import Path

APP_NAME = "arm"


def data_dir() -> Path:
    """返回数据目录，不存在则创建。"""
    d = Path.home() / f".{APP_NAME}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "arm.db"


def lock_path() -> Path:
    return data_dir() / "arm.lock"


def log_path() -> Path:
    return data_dir() / "arm.log"


def config_path() -> Path:
    return data_dir() / "config.toml"
