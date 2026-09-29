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

# 默认探测目标（2026-09-29 修正）：单靠 1.1.1.1 在国内网络会间歇超时
# （实测延迟 234ms，0.25s 超时贴线误报"网络不可用"）。改为多目标、国内优先，
# 任一成功即 up；ARM_NETWORK_PROBE_HOST 环境变量可整体覆盖。
_DEFAULT_TARGETS: list[tuple[str, int]] = [
    ("www.baidu.com", 443),  # DNS 解析 + TCP（国内，含 DNS 层健康）
    ("223.5.5.5", 53),       # AliDNS over TCP（国内，绕开 DNS 只测路由）
    ("1.1.1.1", 443),        # 国外兜底（Cloudflare）
]


def snapshot(cache_s: float = 5.0, timeout_s: float = 0.8) -> dict:
    """返回 ``status`` (up/down/unknown)、原因与探测耗时。

    多目标 TCP 探测，任一成功即视为网络可用；全部失败才判 down。结果短缓存，
    保证引擎 2 秒节拍不会被网络调用拖住。
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if cached is not None and now - float(_cache.get("at", 0.0)) < cache_s:
        return dict(cached)  # type: ignore[arg-type]
    override = os.environ.get("ARM_NETWORK_PROBE_HOST")
    if override:
        try:
            port = int(os.environ.get("ARM_NETWORK_PROBE_PORT", "443"))
        except ValueError:
            port = 443
        targets = [(override, port)]
    else:
        targets = _DEFAULT_TARGETS

    started = time.monotonic()
    result = {"status": "unknown", "up": None, "host": None, "port": None,
              "latency_ms": None, "reason": "未探测"}
    failures: list[str] = []
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                result.update(status="up", up=True, host=host, port=port,
                              reason=f"{host}:{port} TCP 可达",
                              latency_ms=round((time.monotonic() - started) * 1000, 1))
                break
        except (TimeoutError, socket.timeout):
            failures.append(f"{host}:{port} 超时")
        except OSError as exc:
            failures.append(f"{host}:{port} {exc.__class__.__name__}")
        except Exception as exc:
            failures.append(f"{host}:{port} 异常 {exc.__class__.__name__}")
    else:
        result.update(status="down", up=False, host=targets[0][0],
                      port=targets[0][1],
                      reason="全部探测失败: " + "; ".join(failures))
    _cache.update(at=now, data=result)
    return dict(result)


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
    DNS 解析 → 公网 TCP。目标国内优先多候选，防单一境外目标间歇
    超时造成误报（1.1.1.1/chatgpt.com 在国内均不可靠，2026-09-29）。
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
    # DNS：任一候选能解析即视为解析器链路健康（chatgpt.com 国内可能被污染，
    # baidu 作国内基准——两者都失败才算 DNS 层故障）
    dns_ok = False
    dns_fail = []
    for host in ("www.baidu.com", "chatgpt.com"):
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            dns_ok = True
        except OSError as exc:
            dns_fail.append(f"{host}:{exc.__class__.__name__}")
    out.append({"check": "DNS 解析", "ok": dns_ok,
                "detail": "解析成功" if dns_ok else f"全部失败 {', '.join(dns_fail)}"})
    # 公网 TCP：任一候选通即视为可用（多候选防境外目标间歇超时）
    pub_hits = []
    for host, port in (("www.baidu.com", 443), ("1.1.1.1", 443)):
        pub = probe(host, port, 0.6)
        if pub["ok"]:
            pub_hits.append(f"{host}:{port} {pub['latency_ms']}ms")
    out.append({"check": "公网 TCP", "ok": bool(pub_hits),
                "detail": "、".join(pub_hits) if pub_hits else "全部候选不可达"})
    return out
