"""Tests for the centralized SSRF security policy (matricula_toolkit.security)."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from matricula_toolkit.security import (
    SecurityError,
    is_allowed_host,
    validate_host,
    validate_redirect_url,
    validate_url,
)

# ------------------------------------------------------------------
# validate_url
# ------------------------------------------------------------------


def test_validate_url_https_only():
    """Only https scheme is allowed."""
    with pytest.raises(SecurityError, match="Only https scheme"):
        validate_url("http://example.com/")
    with pytest.raises(SecurityError, match="Only https scheme"):
        validate_url("ftp://example.com/")


def test_validate_url_port_443_only():
    """Only port 443 is allowed."""
    with pytest.raises(SecurityError, match="Only port 443"):
        validate_url("https://example.com:8080/")
    with pytest.raises(SecurityError, match="Only port 443"):
        validate_url("https://example.com:80/")
    # No port defaults to 443 for https — allowed.
    assert validate_url("https://example.com/") == "https://example.com/"
    assert validate_url("https://example.com:443/") == "https://example.com:443/"


def test_validate_url_no_credentials():
    """Userinfo/credentials are not allowed."""
    with pytest.raises(SecurityError, match="credentials"):
        validate_url("https://user:pass@example.com/")


def test_validate_url_no_ip_literal():
    """IP literal hosts are rejected."""
    with pytest.raises(SecurityError, match="IP literal"):
        validate_url("https://10.0.0.1/")
    with pytest.raises(SecurityError, match="IP literal"):
        validate_url("https://127.0.0.1/")


def test_validate_url_empty():
    """Empty URL is rejected."""
    with pytest.raises(SecurityError, match="URL is empty"):
        validate_url("")
    with pytest.raises(SecurityError, match="URL is empty"):
        validate_url("   ")


def test_validate_url_valid_public_host(monkeypatch):
    """Valid https URL on a public host is accepted."""

    # Monkeypatch socket.getaddrinfo to return a public IP.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        result = validate_url("https://example.com/")
        assert result == "https://example.com/"


# ------------------------------------------------------------------
# validate_host
# ------------------------------------------------------------------


def test_validate_host_private_ipv4():
    """Private IPv4 hostnames are rejected."""
    with pytest.raises(SecurityError):
        validate_host("10.0.0.1")
    with pytest.raises(SecurityError):
        validate_host("172.16.0.1")
    with pytest.raises(SecurityError):
        validate_host("192.168.1.1")
    with pytest.raises(SecurityError):
        validate_host("127.0.0.1")
    with pytest.raises(SecurityError):
        validate_host("169.254.0.1")


def test_validate_host_private_ipv6():
    """Private IPv6 hostnames are rejected."""
    with pytest.raises(SecurityError):
        validate_host("::1")
    with pytest.raises(SecurityError):
        validate_host("fc00::1")
    with pytest.raises(SecurityError):
        validate_host("fe80::1")


def test_validate_host_cgnat():
    """CGNAT range is denied."""
    with pytest.raises(SecurityError):
        validate_host("100.64.0.1")


def test_validate_host_test_net():
    """TEST-NET ranges are denied."""
    with pytest.raises(SecurityError):
        validate_host("192.0.2.1")
    with pytest.raises(SecurityError):
        validate_host("198.51.100.1")
    with pytest.raises(SecurityError):
        validate_host("203.0.113.1")


def test_validate_host_multicast():
    """Multicast ranges are denied."""
    with pytest.raises(SecurityError):
        validate_host("224.0.0.1")
    with pytest.raises(SecurityError):
        validate_host("ff00::1")


def test_validate_host_reserved():
    """Reserved ranges are denied."""
    with pytest.raises(SecurityError):
        validate_host("240.0.0.1")
    with pytest.raises(SecurityError):
        validate_host("0.0.0.1")


def test_validate_host_empty():
    """Empty host is rejected."""
    with pytest.raises(SecurityError, match="Host is empty"):
        validate_host("")
    with pytest.raises(SecurityError, match="Host is empty"):
        validate_host("   ")


def test_validate_host_idna(monkeypatch):
    """Internationalized domain names are IDNA-encoded."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        result = validate_host("münchen.example")
        assert result == "xn--mnchen-3ya.example"


# ------------------------------------------------------------------
# DNS rebinding protection
# ------------------------------------------------------------------


def test_dns_rebinding_mixed_public_private(monkeypatch):
    """Mixed public/private DNS results are rejected (rebinding protection)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo),
        pytest.raises(SecurityError),
    ):
        validate_host("rebinding.example")


def test_dns_rebinding_all_private(monkeypatch):
    """All-private DNS results are rejected."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.16.0.1", 443)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo),
        pytest.raises(SecurityError),
    ):
        validate_host("evil.internal.example")


def test_dns_failure_fail_closed(monkeypatch):
    """DNS resolution failure fails closed (host treated as blocked)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise OSError("DNS failure")

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo),
        pytest.raises(SecurityError, match="DNS resolution failed"),
    ):
        validate_host("nonexistent.example")


def test_dns_no_addresses_fail_closed(monkeypatch):
    """DNS resolution with no addresses fails closed."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return []

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo),
        pytest.raises(SecurityError, match="No addresses resolved"),
    ):
        validate_host("noaddr.example")


# ------------------------------------------------------------------
# is_allowed_host
# ------------------------------------------------------------------


def test_is_allowed_host_public(monkeypatch):
    """Public host is allowed."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert is_allowed_host("example.com") is True


def test_is_allowed_host_private(monkeypatch):
    """Private host is not allowed."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert is_allowed_host("10.0.0.1") is False


def test_is_allowed_host_dns_failure(monkeypatch):
    """DNS failure means not allowed (fail-closed)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise OSError("DNS failure")

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert is_allowed_host("nonexistent.example") is False


# ------------------------------------------------------------------
# validate_redirect_url
# ------------------------------------------------------------------


def test_validate_redirect_url_same_as_validate_url():
    """validate_redirect_url applies the same policy as validate_url."""
    with pytest.raises(SecurityError):
        validate_redirect_url("https://example.com/", "http://evil.com/")

    with pytest.raises(SecurityError):
        validate_redirect_url("https://example.com/", "https://10.0.0.1/")

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        result = validate_redirect_url("https://example.com/", "https://safe.example/")
        assert result == "https://safe.example/"
