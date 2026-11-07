# -*- coding: utf-8 -*-
"""出站地址安全校验（SSRF 防护）。

普通登录用户即可在「我的 → 通知设置」填写 Bark / Webhook 出站地址，若不加约束，
可驱动服务器访问云元数据（169.254.169.254）、回环（127.0.0.1）或自身管理口。
这里只拦三类明确危险的网段，保留 192.168/10.x 等自建 Bark / n8n / HA 场景。
"""
import ipaddress
import socket
from urllib.parse import urlparse

# 被拦网段：链路本地 / 回环 / 未指定地址（含 IPv6 回环）
_BLOCKED_NETS = (
    ipaddress.ip_network("169.254.0.0/16", strict=False),
    ipaddress.ip_network("127.0.0.0/8", strict=False),
    ipaddress.ip_network("::1/128", strict=False),
    ipaddress.ip_network("0.0.0.0/8", strict=False),
)

_ALLOWED_SCHEMES = ("http", "https")


def assert_safe_outbound(url: str) -> None:
    """校验出站 URL 不指向被拦网段；非法 scheme、无法解析或命中即抛 ValueError。

    纯标准库，便于 CI 纯标准库测试直接调用验证。
    """
    if not url:
        raise ValueError("地址为空")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError("仅支持 http(s) 地址")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError("域名无法解析")
    for info in infos:
        host = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if any(ip in net for net in _BLOCKED_NETS):
            raise ValueError("不允许访问该地址")
