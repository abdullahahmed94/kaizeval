"""Outbound network policy for model and agent endpoints.

Two goals:

* Offline mode is a hard boundary. A destination that is not loopback, private, or explicitly
  allowlisted is refused *before* any DNS lookup or HTTP request is made.
* Connected mode still refuses link-local / metadata addresses (classic SSRF targets) and only
  reaches public hosts for endpoints the workspace owner explicitly marked as allowed.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


class EgressBlocked(Exception):
    """Raised when a destination violates the network policy."""

    def __init__(self, reason: str, url: str):
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


@dataclass(frozen=True)
class EgressPolicy:
    offline: bool
    allow_public: bool = False
    allowlist: frozenset[str] = frozenset()


def _classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "forbidden"
    if ip.is_private:
        return "private"
    return "public"


def _is_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def check_url(url: str, policy: EgressPolicy) -> str:
    """Validate ``url`` against ``policy``. Returns the destination class or raises EgressBlocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise EgressBlocked("unsupported URL scheme", url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressBlocked("missing host", url)

    literal = _is_ip_literal(host)
    if literal is not None:
        kinds = {_classify_ip(literal)}
    elif host == "localhost" or host.endswith(".localhost"):
        kinds = {"loopback"}
    elif host in policy.allowlist:
        # The operator declared this name internal; do not resolve it here.
        return "allowlisted"
    elif policy.offline:
        if "." in host:
            # A dotted name could be public. Refuse without resolving so that offline mode
            # never emits a DNS query for an external name.
            raise EgressBlocked("offline mode: host is not local or allowlisted", url)
        # Single-label names (for example a compose service name) resolve on the local network.
        return "internal-name"
    else:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except OSError as exc:
            raise EgressBlocked(f"cannot resolve host ({exc})", url) from exc
        kinds = {_classify_ip(ipaddress.ip_address(info[4][0])) for info in infos}

    if "forbidden" in kinds:
        raise EgressBlocked("link-local, multicast or reserved address", url)
    if "public" in kinds:
        if policy.offline:
            raise EgressBlocked("offline mode: public address", url)
        if not policy.allow_public:
            raise EgressBlocked("public destination not allowed for this endpoint", url)
        return "public"
    return "loopback" if kinds == {"loopback"} else "private"


def guarded_client(policy: EgressPolicy, *, timeout: float = 30.0, headers: dict | None = None) -> httpx.Client:
    """An httpx client that re-checks every request URL and never follows redirects."""

    def _hook(request: httpx.Request) -> None:
        check_url(str(request.url), policy)

    return httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=False,
        trust_env=False,  # ignore HTTP(S)_PROXY so traffic cannot be silently re-routed
        event_hooks={"request": [_hook]},
    )
