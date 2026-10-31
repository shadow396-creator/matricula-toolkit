"""Integration tests for the local SSRF proxy (matricula_toolkit.local_proxy)."""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import patch

import pytest

from matricula_toolkit.local_proxy import LocalProxy

# ------------------------------------------------------------------
# Proxy lifecycle
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_starts_on_loopback():
    """Proxy binds to a loopback interface with an ephemeral port."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        assert proxy.proxy_url.startswith(
            "http://127.0.0.1:"
        ) or proxy.proxy_url.startswith("http://[::1]:")
        port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))
        assert port > 0
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_stop_idempotent():
    """Stopping an already-stopped proxy is safe."""
    proxy = LocalProxy()
    await proxy.stop()  # Should not raise
    await proxy.start()
    await proxy.stop()
    await proxy.stop()  # Double stop should not raise


# ------------------------------------------------------------------
# CONNECT target validation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_connect_rejects_private_host():
    """CONNECT to a private host is rejected (403)."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port(proxy))
        try:
            writer.write(b"CONNECT 10.0.0.1:443 HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n")
            await writer.drain()
            response = await reader.readline()
            assert b"403" in response
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_rejects_non_443_port():
    """CONNECT to a non-443 port is rejected (403)."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port(proxy))
        try:
            writer.write(
                b"CONNECT example.com:8080 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            await writer.drain()
            response = await reader.readline()
            assert b"403" in response
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_rejects_dns_failure():
    """CONNECT to a host that fails DNS resolution is rejected (403)."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port(proxy))
        try:
            writer.write(
                b"CONNECT nonexistent.invalid:443 HTTP/1.1\r\nHost: nonexistent.invalid\r\n\r\n"
            )
            await writer.drain()
            response = await reader.readline()
            assert b"403" in response
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_timeout_returns_502():
    """CONNECT to a host whose upstream connection times out is rejected (502).

    The proxy enforces CONNECT_TIMEOUT_SECONDS on the upstream TCP connect.
    If the validated host does not accept the connection within that window,
    the handler must return 502 Bad Gateway instead of hanging indefinitely.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

        public_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

        real_open_connection = asyncio.open_connection

        async def slow_open_connection(host, port, *args, **kwargs):
            # Don't intercept the client-to-proxy connection.
            if port == proxy_port_num:
                return await real_open_connection(host, port, *args, **kwargs)
            # Simulate a slow/non-responsive upstream: raise TimeoutError
            # as asyncio.wait_for would after CONNECT_TIMEOUT_SECONDS.
            raise asyncio.TimeoutError("Upstream connect timed out")

        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                lambda host, port, *args, **kwargs: public_addr,
            ),
            patch(
                "matricula_toolkit.local_proxy.asyncio.open_connection",
                slow_open_connection,
            ),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port_num)
            try:
                writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), timeout=10)
                assert b"502" in response, (
                    f"Proxy should return 502 on connect timeout, got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------


def proxy_port(proxy: LocalProxy) -> int:
    """Extract the port from the proxy URL."""
    return int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))


# ------------------------------------------------------------------
# F3/F6: Tunnel I/O timeouts (read + drain)
# ------------------------------------------------------------------


class _BlockingStreamReader:
    """StreamReader that blocks forever on read()."""

    async def read(self, n: int) -> bytes:
        # Never returns — simulates a stalled upstream.
        await asyncio.Event().wait()
        return b""


class _BlockingDrainWriter:
    """StreamWriter whose drain() blocks forever."""

    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        # Never returns — simulates a stalled drain.
        await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _DataThenEOFReader:
    """StreamReader that returns some data, then EOF."""

    def __init__(self, data: bytes = b"hello") -> None:
        self._data = data
        self._eof = False

    async def read(self, n: int) -> bytes:
        if self._eof:
            return b""
        if self._data:
            data, self._data = self._data, b""
            return data
        self._eof = True
        return b""


@pytest.mark.asyncio
async def test_proxy_tunnel_read_timeout(monkeypatch):
    """F3: _proxy_tunnel must not hang forever on a stalled src.read().

    When the upstream stops sending data, the read pump must time out
    after TUNNEL_IO_TIMEOUT_SECONDS and clean up (close the writer).
    """
    from matricula_toolkit import local_proxy

    monkeypatch.setattr(local_proxy, "TUNNEL_IO_TIMEOUT_SECONDS", 0.1)

    reader = _BlockingStreamReader()
    writer = _BlockingDrainWriter()

    # Both directions use the same blocked reader/writer.
    await local_proxy.LocalProxy._proxy_tunnel(reader, writer, reader, writer)

    # The writer must have been closed (cleanup happened despite the hang).
    assert writer.closed, "Writer should be closed after read timeout"


@pytest.mark.asyncio
async def test_proxy_tunnel_drain_timeout(monkeypatch):
    """F6: _proxy_tunnel must not hang forever on a stalled dst.drain().

    When the downstream stops accepting data, the drain must time out
    after TUNNEL_IO_TIMEOUT_SECONDS and clean up (close the writer).
    """
    from matricula_toolkit import local_proxy

    monkeypatch.setattr(local_proxy, "TUNNEL_IO_TIMEOUT_SECONDS", 0.1)

    reader = _DataThenEOFReader()
    writer = _BlockingDrainWriter()

    await local_proxy.LocalProxy._proxy_tunnel(reader, writer, reader, writer)

    assert writer.closed, "Writer should be closed after drain timeout"


# ------------------------------------------------------------------
# F8: Redirect limit constant
# ------------------------------------------------------------------


def test_max_redirects_constant():
    """F8: MAX_REDIRECTS must not exist as a misleading constant.

    The redirect limit is enforced by the LocalProxy at the CONNECT level
    (403 for denied targets), not by an application-level constant that
    could give a false sense of protection.
    """
    import matricula_toolkit.local_proxy as lp

    assert not hasattr(lp, "MAX_REDIRECTS"), (
        "MAX_REDIRECTS should not exist as an unused constant"
    )
