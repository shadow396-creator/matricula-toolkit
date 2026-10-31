"""Centralized deny-by-default SSRF security policy.

This module is the single source of truth for host/URL validation across the
Matricula Toolkit.  It implements a *fail-closed* policy:

* Only the ``https`` scheme is allowed.
* Only port 443 is allowed (no custom ports).
* No userinfo/credentials in URLs.
* No IP literals (hostnames only, resolved via DNS).
* Internationalized domain names are IDNA-encoded before validation.
* Every DNS-resolved address must be public; any private/special/resolved
  address causes rejection (DNS-rebinding protection).
* IPv4-mapped IPv6 addresses are expanded and checked as IPv4.
* DNS resolution failures fail closed (the host is treated as blocked).

All network paths (browser navigation, image downloads, API requests) must
route through this policy.  Callers must never fall back to a direct
connection when this policy rejects a target.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from matricula_toolkit.exceptions import MatriculaError


class SecurityError(MatriculaError):
    """Raised when a URL or host violates the SSRF security policy."""


# ---------------------------------------------------------------------------
# Deny-by-default network ranges (IPv4 + IPv6)
#
# Every address that is not globally routable public space is denied.  This
# list is intentionally comprehensive: private, loopback, link-local,
# CGNAT, TEST-NET, documentation, multicast, reserved, unspecified and
# IPv4-mapped IPv6 ranges are all covered.
# ---------------------------------------------------------------------------

_DENY_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4 private / loopback / link-local
    ipaddress.ip_network("0.0.0.0/8"),  # this network / unspecified
    ipaddress.ip_network("10.0.0.0/8"),  # private
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("172.16.0.0/12"),  # private
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),  # 6to4 relay (deprecated)
    ipaddress.ip_network("192.168.0.0/16"),  # private
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("255.255.255.255/32"),  # limited broadcast
    # IPv6 loopback / link-local / unique-local / multicast / unspecified
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("::1/128"),  # loopback
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6: entire range denied
    # (embedded IPv4 is checked separately
    # via _expand_ipv4_mapped in _validate_addresses)
    ipaddress.ip_network("64:ff9b::/96"),  # IPv4-IPv6 translation
    ipaddress.ip_network("100::/64"),  # discard-only
    ipaddress.ip_network("2001::/32"),  # Teredo
    ipaddress.ip_network("2001:2::/48"),  # benchmarking
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("2002::/16"),  # 6to4
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
]


def _is_denied_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* falls inside any deny network."""
    return any(addr in net for net in _DENY_NETWORKS)


def _expand_ipv4_mapped(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the embedded IPv4 address for an IPv4-mapped IPv6 address, else None."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return None


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """DNS-resolve *host* and return every unique address.

    Raises :class:`SecurityError` if resolution fails or yields no addresses.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise SecurityError(f"DNS resolution failed for {host!r}: {exc}") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canonname, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        key = str(addr)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(addr)

    if not addresses:
        raise SecurityError(f"No addresses resolved for {host!r}")

    return addresses


def _validate_addresses(
    host: str, addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
) -> None:
    """Reject if any resolved address is denied (DNS-rebinding protection).

    All addresses must be public.  A mix of public and private addresses is
    rejected outright, because a rebinding attack could switch between them.
    """
    for addr in addresses:
        # Expand IPv4-mapped IPv6 and check the embedded IPv4 too.
        if isinstance(addr, ipaddress.IPv6Address):
            mapped = _expand_ipv4_mapped(addr)
        else:
            mapped = None
        check_addrs = [addr] + ([mapped] if mapped is not None else [])
        for check_addr in check_addrs:
            if _is_denied_address(check_addr):
                raise SecurityError(
                    f"Host {host!r} resolves to denied address {check_addr}"
                )


def is_allowed_host(host: str) -> bool:
    """Return True if *host* is allowed by the security policy.

    This is a convenience predicate; :func:`validate_host` is the
    fail-closed variant that raises :class:`SecurityError` on denial.
    """
    try:
        validate_host(host)
        return True
    except SecurityError:
        return False


def validate_host(host: str) -> str:
    """Validate *host* and return its IDNA-encoded (ASCII) form.

    Raises :class:`SecurityError` if the host is an IP literal, fails DNS
    resolution, or resolves to any denied address.
    """
    if not host or not host.strip():
        raise SecurityError("Host is empty")

    # Reject IP literals directly — only hostnames may be used.
    try:
        ipaddress.ip_address(host)
        raise SecurityError(f"IP literal hosts are not allowed: {host!r}")
    except ValueError:
        pass

    # IDNA-encode internationalized domain names to ASCII.
    try:
        encoded = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise SecurityError(f"Invalid hostname {host!r}: {exc}") from exc

    # DNS-resolve and validate every address (fail closed).
    addresses = _resolve_host(encoded)
    _validate_addresses(encoded, addresses)

    return encoded


def validate_url(url: str) -> str:
    """Validate a full URL and return it unchanged if allowed.

    Raises :class:`SecurityError` for any policy violation:
    non-https scheme, non-443 port, credentials, IP-literal host, empty
    hostname, DNS failure, or denied resolved address.
    """
    if not url or not url.strip():
        raise SecurityError("URL is empty")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SecurityError(f"Invalid URL {url!r}: {exc}") from exc

    # Scheme must be https.
    if parsed.scheme != "https":
        raise SecurityError(f"Only https scheme is allowed, got {parsed.scheme!r}")

    # No credentials (user:pass@host).
    if parsed.username is not None or parsed.password is not None:
        raise SecurityError("URL credentials (userinfo) are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("URL must include a hostname")

    # Port: only 443 (or no port, which defaults to 443 for https).
    if parsed.port is not None and parsed.port != 443:
        raise SecurityError(f"Only port 443 is allowed, got {parsed.port}")

    # Validate the host (IDNA + DNS + deny ranges).
    validate_host(hostname)

    return url


def validate_redirect_url(original_url: str, redirect_url: str) -> str:
    """Validate a redirect target URL with the same policy as :func:`validate_url`.

    The *original_url* is accepted for context/auditing but is not used to
    relax the policy — each redirect target is independently validated.
    """
    return validate_url(redirect_url)
