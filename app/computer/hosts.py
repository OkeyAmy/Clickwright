"""Host scoping.

An agent that will drive any URL you hand it is a liability unless it is fenced.
A connector is scoped to the hosts it was compiled against: navigation outside
that set is refused, so a poisoned link or a redirect chain cannot walk the
browser somewhere the operator never authorised.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

# A hard ceiling applied on top of every connector's own scope, if configured.
GLOBAL_ALLOWLIST = [h.strip().lower() for h in os.getenv("TARGET_ALLOWED_HOSTS", "").split(",") if h.strip()]


class HostRefused(RuntimeError):
    pass


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_private(url_or_host: str) -> bool:
    """True for localhost and RFC1918-style addresses.

    Used to decide whether ADK's private-network guard needs relaxing — which
    should happen for a local target and never for a public one.
    """
    host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (socket.gaierror, ValueError):
            return False
    return address.is_private or address.is_loopback or address.is_link_local


def normalise(hosts: list[str] | None, fallback_url: str | None = None) -> list[str]:
    """The scope for a connector: what was asked for, or the target's own host."""
    scope = [h.strip().lower() for h in (hosts or []) if h.strip()]
    if not scope and fallback_url:
        host = host_of(fallback_url)
        if host:
            scope = [host]
    return scope


def matches(host: str, allowed: str) -> bool:
    """Exact host, or a leading-dot suffix rule (".example.com" covers subdomains)."""
    host, allowed = host.lower(), allowed.lower()
    if allowed.startswith("."):
        return host == allowed[1:] or host.endswith(allowed)
    return host == allowed


def check(url: str, allowed_hosts: list[str]) -> None:
    """Raises HostRefused unless the URL is inside scope. Silence would be worse:
    the caller needs to know the agent tried to leave."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HostRefused(f"refused {url!r}: only http and https are allowed")

    host = (parsed.hostname or "").lower()
    if not host:
        raise HostRefused(f"refused {url!r}: no host")

    if allowed_hosts and not any(matches(host, rule) for rule in allowed_hosts):
        raise HostRefused(
            f"refused {host!r}: outside this connector's scope ({', '.join(allowed_hosts)})"
        )

    if GLOBAL_ALLOWLIST and not any(matches(host, rule) for rule in GLOBAL_ALLOWLIST):
        raise HostRefused(f"refused {host!r}: not in TARGET_ALLOWED_HOSTS")
