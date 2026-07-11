# 프록시(Caddy) 뒤에서 X-Forwarded-For/Proto 를 안전하게 해석하는 요청 유틸
from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request


@lru_cache(maxsize=8)
def _parse_networks(allow_ips: str) -> tuple[ipaddress._BaseNetwork, ...]:
    nets: list[ipaddress._BaseNetwork] = []
    for token in allow_ips.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _is_trusted(ip: str, allow_ips: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _parse_networks(allow_ips))


def client_ip(request: Request, allow_ips: str) -> str:
    """실제 클라이언트 IP 를 산출한다.

    직접 접속(peer)이 신뢰 프록시일 때만 X-Forwarded-For 의 **가장 오른쪽에서
    신뢰 체인을 벗어난 첫 IP**(=최초 외부 클라이언트)를 채택한다. 신뢰 프록시가
    아니면 XFF 를 무시하고 peer IP 를 그대로 쓴다(스푸핑 방지).
    """
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted(peer, allow_ips):
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    # 오른쪽(프록시에 가까운 쪽)부터 훑어 신뢰 프록시를 건너뛰고 첫 비신뢰 IP 를 클라이언트로.
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    for ip in reversed(parts):
        if not _is_trusted(ip, allow_ips):
            return ip
    # 모든 홉이 신뢰 프록시면 가장 왼쪽(최초 발신)을 채택.
    return parts[0] if parts else peer


def is_https(request: Request, allow_ips: str) -> bool:
    """요청이 https(TLS)로 왔는지 판정한다.

    신뢰 프록시(Caddy) 뒤에서는 X-Forwarded-Proto 를 신뢰한다. 프록시 없이 직접
    온 요청은 request.url.scheme 을 본다. 값이 없으면(헤더 누락) http 로 간주해
    보수적으로 거부한다.
    """
    peer = request.client.host if request.client else "unknown"
    if _is_trusted(peer, allow_ips):
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"
