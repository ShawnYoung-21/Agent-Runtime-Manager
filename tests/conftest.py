"""pytest 共享配置。

本机 Windows 下 pytest 默认 basetemp（%TEMP%\\pytest-of-<user>）出现权限拒绝，
故把 basetemp 指到项目内 .pytest_tmp，并为 tmp_path 提供干净根。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".pytest_tmp"


@pytest.fixture()
def tmp_path(request):  # type: ignore[override]
    """覆盖内置 tmp_path：用项目内目录，规避 %TEMP% 权限问题。"""
    node = request.node.name.replace(":", "_").replace("[", "_").replace("]", "_")
    path = _TMP_ROOT / node
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)
