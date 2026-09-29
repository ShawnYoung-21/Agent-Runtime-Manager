"""轻量网络连通性传感器。

网络状态只用于提示与诊断，不参与保护释放：网络短暂抖动时仍须保护正在运行的 Agent。
"""

from __future__ import annotations

import os
import socket
import time
from typing import Optional

_cache: dict[str, object] = {"at": 0.0, "data": None}
_gateway_cache: dict[str, object] = {"at": 0.0, "data": None}


def default_gateway(cache_s: float = 60.0) -> Optional[str]:
    """读 IPv4 默认网关（route print 解析，结果短缓存）。

    只读、失败返回 None。网关 IP 是数字点分格式，不受控制台语言影响。
    """
    import re
    import subprocess

    now = time.monotonic()
    cached = _gateway_cache.get("data")
    if cached is not None and now - float(_gateway_cache.get("at", 0.0)) < cache_s:
        return cached  # type: ignore[return-value]
    gw = None
    try:
        # route 的 / 参数不能走 Git Bash 直调（MSYS 吃参数），这里由
        # Python subprocess 列表参数直调，符合项目红线
        r = subprocess.run(["route", "print", "-4", "0.0.0.0"],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                      r.stdout or "")
        if m:
            gw = m.group(1)
    except Exception:
        gw = None
    _gateway_cache.update(at=now, data=gw)
    return gw


def probe(host: str, port: int = 443, timeout_s: float = 0.5) -> dict:
    """单点 TCP 探测，返回 {ok, latency_ms, reason}（供分层诊断用）。"""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "reason": "TCP 可达"}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "latency_ms": None, "reason": "TCP 超时"}
    except OSError as exc:
        return {"ok": False, "latency_ms": None,
                "reason": f"失败: {exc.__class__.__name__}"}


def diagnose() -> list[dict]:
    """网络分层诊断（合盖报告/排障用，非 2s 热路径）。

    阶梯：默认网关(ICMP, 仅供参考——ICMP 可能被防火墙拦) →
    DNS 解析 → 公网 TCP。区分"网关断/DNS 坏/公网断"三种故障层。
    """
    out: list[dict] = []
    gw = default_gateway()
    if gw:
        started = time.monotonic()
        try:
            import subprocess

            r = subprocess.run(["ping", "-n", "1", "-w", "500", gw],
                               capture_output=True, timeout=5)
            gw_ok = r.returncode == 0
        except Exception:
            gw_ok = False
        out.append({"check": "网关连通(参考)", "ok": None,
                    "detail": f"{gw} ping {'通过' if gw_ok else '不通'}"
                              f" ({round((time.monotonic()-started)*1000)}ms)"})
    else:
        out.append({"check": "网关连通(参考)", "ok": None, "detail": "未找到默认网关"})
    # DNS：能解析说明本机解析器到递归 DNS 的链路活着
    try:
        socket.getaddrinfo("chatgpt.com", 443, type=socket.SOCK_STREAM)
        out.append({"check": "DNS 解析", "ok": True, "detail": "chatgpt.com 解析成功"})
    except OSError as exc:
        out.append({"check": "DNS 解析", "ok": False,
                    "detail": f"解析失败: {exc.__class__.__name__}"})
    # 公网 TCP：最终判定层
    pub = probe("1.1.1.1", 443, 0.5)
    out.append({"check": "公网 TCP", "ok": pub["ok"],
                "detail": f"1.1.1.1:443 {pub['reason']}"})
    return out


def snapshot(cache_s: float = 5.0, timeout_s: float = 0.25) -> dict:
    """返回 ``status`` (up/down/unknown)、原因与探测耗时。

    使用 TCP 探测而不是 ping，避免管理员权限和 ICMP 被禁造成假阴性；探测结果短缓存，
    保证引擎 2 秒节拍不会被网络调用拖住。
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if cached is not None and now - float(_cache.get("at", 0.0)) < cache_s:
        return dict(cached)  # type: ignore[arg-type]
    host = os.environ.get("ARM_NETWORK_PROBE_HOST", "1.1.1.1")
    try:
        port = int(os.environ.get("ARM_NETWORK_PROBE_PORT", "443"))
    except ValueError:
        port = 443
    started = time.monotonic()
    result = {"status": "unknown", "up": None, "host": host, "port": port,
              "latency_ms": None, "reason": "未探测"}
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            result.update(status="up", up=True, reason="TCP 探测成功")
    except (TimeoutError, socket.timeout):
        result.update(status="down", up=False, reason="TCP 探测超时")
    except OSError as exc:
        result.update(status="down", up=False, reason=f"TCP 探测失败: {exc.__class__.__name__}")
    except Exception as exc:
        result["reason"] = f"探测异常: {exc.__class__.__name__}"
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    _cache.update(at=now, data=result)
    return dict(result)
