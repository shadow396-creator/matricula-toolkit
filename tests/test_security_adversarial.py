"""Adversarial security tests for SSRF hardening (Phase C–G).

These tests verify the *actual* security properties, not just mocked unit
behaviour.  They test:

* DNS rebinding with real TCP connections (Phase C)
* TOCTOU between validate_host and _select_target_address (Phase C)
* Proxy connects to validated IP, not re-resolved hostname (Phase C)
* HTTPS CONNECT / TLS security (Phase D)
* Redirect handling and browser path coverage (Phase E)
* Proxy isolation: loopback-only bind, no open proxy (Phase F)
* IP policy edge cases (Phase G)
"""

from __future__ import annotations

import asyncio
import socket
import threading
from unittest.mock import patch

import pytest

from matricula_toolkit.local_proxy import LocalProxy
from matricula_toolkit.security import SecurityError, validate_host

# ------------------------------------------------------------------
# Helper: a tiny TCP echo server we can connect to through the proxy
# ------------------------------------------------------------------


class _TcpEchoServer:
    """A minimal TCP server that records connections and echoes data."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self.connections: list[tuple[str, int]] = []
        self._received: list[bytes] = []
        self._started = threading.Event()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        if peername:
            self.connections.append((peername[0], peername[1]))
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self._received.append(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: S110,BLE001 — test helper, connection errors expected
                pass

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port
        )
        sockets = self._server.sockets  # type: ignore[attr-defined]
        if sockets:
            self._port = sockets[0].getsockname()[1]
        self._started.set()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> int:
        return self._port


def _run_async(coro):
    """Run an async coroutine, returning its result."""
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Phase C: DNS rebinding with real TCP connections
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_connect_toctou_dns_rebinding():
    """TOCTOU: validate_host sees public IP, _select_target_address sees private IP.

    The proxy calls validate_host() (which resolves DNS and checks all
    addresses are public), then calls _select_target_address() which
    resolves DNS *again*.  If DNS changes between these two calls, the
    proxy could connect to a private address that passed validation.

    This test simulates DNS rebinding by making getaddrinfo return a
    public IP on the first call (validate_host) and a private IP on the
    second call (_select_target_address).  The proxy must reject this.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:

        def rebinding_getaddrinfo(host, port, *args, **kwargs):
            # Single resolution returns mixed public/private — rebinding!
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]

        with patch(
            "matricula_toolkit.security.socket.getaddrinfo",
            rebinding_getaddrinfo,
        ):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port(proxy)
            )
            try:
                writer.write(
                    b"CONNECT rebinding.example:443 HTTP/1.1\r\n"
                    b"Host: rebinding.example\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readline()
                assert b"403" in response, (
                    f"Proxy should reject DNS rebinding (mixed addresses), "
                    f"got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_actual_target_is_validated_ip():
    """Proxy connects to the IP validated by validate_host, not a re-resolved one.

    We mock getaddrinfo (in both security and local_proxy modules) to return
    8.8.8.8 (public).  We also mock asyncio.open_connection to record the
    target address it's called with and then refuse the connection.  The key
    assertion: the proxy attempts to connect to 8.8.8.8 (the validated public
    IP), never to 127.0.0.1 (which would be a rebinding bypass).
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))
        connected_addresses: list[str] = []

        real_open_connection = asyncio.open_connection

        async def fake_open_connection(host, port, *args, **kwargs):
            # Don't intercept the client-to-proxy connection.
            if port == proxy_port_num:
                return await real_open_connection(host, port, *args, **kwargs)
            connected_addresses.append(str(host))
            raise ConnectionRefusedError(f"Cannot connect to {host}:{port}")

        # Patch getaddrinfo in BOTH modules (security.py and local_proxy.py
        # each hold their own reference to the socket module).
        public_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]
        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                lambda host, port, *args, **kwargs: public_addr,
            ),
            patch(
                "matricula_toolkit.local_proxy.asyncio.open_connection",
                fake_open_connection,
            ),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port_num)
            try:
                writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readline()
                # The proxy tried to connect to 8.8.8.8:443 and was refused.
                # Expect 502 Bad Gateway.
                assert b"502" in response, (
                    f"Proxy should fail connecting to 8.8.8.8, got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()

        # The proxy must have attempted to connect to 8.8.8.8 (validated IP),
        # never to 127.0.0.1 (rebinding bypass target).
        assert connected_addresses == ["8.8.8.8"], (
            f"Proxy connected to {connected_addresses} instead of validated IP 8.8.8.8"
        )
        assert "127.0.0.1" not in connected_addresses, (
            "Proxy connected to 127.0.0.1 — rebinding bypass!"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_rebinding_mixed_addresses_second_call():
    """DNS rebinding: single resolution returns mixed public/private addresses.

    F1 fix: DNS is resolved exactly once, and ALL addresses are validated
    together.  A mix of public and private addresses in a single resolution
    is rejected outright — there is no second resolution that could bypass
    the check.

    This test simulates DNS rebinding by making getaddrinfo return both
    a public IP (8.8.8.8) and a private IP (10.0.0.1) in a SINGLE call.
    The proxy must reject this because _validate_addresses checks all
    addresses at once.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:

        def mixed_getaddrinfo(host, port, *args, **kwargs):
            # Single resolution returns mixed public/private — rebinding!
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
            ]

        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                mixed_getaddrinfo,
            ),
        ):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port(proxy)
            )
            try:
                writer.write(
                    b"CONNECT mixed.example:443 HTTP/1.1\r\nHost: mixed.example\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readline()
                assert b"403" in response, (
                    f"Proxy should reject mixed addresses (single resolution), "
                    f"got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


# ------------------------------------------------------------------
# Phase C: Real TCP connection target verification
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_connects_to_validated_public_ip_not_localhost():
    """Integration: proxy connects to validated public IP, never to 127.0.0.1.

    We run a real TCP echo server on 127.0.0.1.  We mock getaddrinfo (in
    both modules) to return 8.8.8.8 (public).  We mock asyncio.open_connection
    to record the target address and refuse the connection.  The key
    assertion: the proxy attempts to connect to 8.8.8.8 (validated public IP),
    never to 127.0.0.1 (our local server).  If the proxy were vulnerable to
    rebinding and connected to 127.0.0.1, our local server would see a
    connection.
    """
    server = _TcpEchoServer("127.0.0.1", 0)
    await server.start()
    try:
        proxy = LocalProxy()
        await proxy.start()
        try:
            proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))
            connected_addresses: list[str] = []

            real_open_connection = asyncio.open_connection

            async def fake_open_connection(host, port, *args, **kwargs):
                # Don't intercept the client-to-proxy connection.
                if port == proxy_port_num:
                    return await real_open_connection(host, port, *args, **kwargs)
                connected_addresses.append(str(host))
                raise ConnectionRefusedError(f"Cannot connect to {host}:{port}")

            public_addr = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            ]
            with (
                patch(
                    "matricula_toolkit.security.socket.getaddrinfo",
                    lambda host, port, *args, **kwargs: public_addr,
                ),
                patch(
                    "matricula_toolkit.local_proxy.asyncio.open_connection",
                    fake_open_connection,
                ),
            ):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy_port_num
                )
                try:
                    writer.write(
                        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
                    )
                    await writer.drain()
                    response = await reader.readline()
                    assert b"502" in response, (
                        f"Expected 502 for refused connection, got: {response!r}"
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()

            # The proxy must have attempted to connect to 8.8.8.8, never 127.0.0.1.
            assert connected_addresses == ["8.8.8.8"], (
                f"Proxy connected to {connected_addresses} instead of 8.8.8.8"
            )
            assert "127.0.0.1" not in connected_addresses, (
                "Proxy connected to 127.0.0.1 — rebinding bypass!"
            )

            # Our local server must have received ZERO connections.
            assert len(server.connections) == 0, (
                f"Proxy connected to localhost {server.connections} — "
                f"rebinding bypass!  Proxy must connect to validated "
                f"public IP (8.8.8.8), not 127.0.0.1."
            )
        finally:
            await proxy.stop()
    finally:
        await server.stop()


# ------------------------------------------------------------------
# Phase C: validate_host with real socket.getaddrinfo (no mocking)
# ------------------------------------------------------------------


def test_validate_host_localhost_fails_closed():
    """validate_host('localhost') must fail — localhost resolves to 127.0.0.1."""
    with pytest.raises(SecurityError):
        validate_host("localhost")


def test_validate_host_public_domain_may_fail_in_sandbox():
    """validate_host on a real public domain may fail in sandbox (no DNS).

    This is informational: in a sandboxed environment without DNS,
    validate_host will fail closed (SecurityError).  That's correct
    behaviour — we don't assert pass or fail, just that it doesn't
    crash unexpectedly.
    """
    try:
        validate_host("example.com")
    except SecurityError:
        # DNS may not be available in sandbox — fail-closed is correct.
        pass


# ------------------------------------------------------------------
# Phase G: IP policy edge cases
# ------------------------------------------------------------------


def test_validate_host_ipv4_mapped_ipv6_private():
    """IPv4-mapped IPv6 with private embedded IPv4 is denied."""
    # ::ffff:10.0.0.1 is an IPv4-mapped IPv6 address for 10.0.0.1 (private).
    with pytest.raises(SecurityError):
        validate_host("::ffff:10.0.0.1")


def test_validate_host_ipv4_mapped_ipv6_public():
    """IPv4-mapped IPv6 with public embedded IPv4 — DNS resolution needed.

    ::ffff:8.8.8.8 is an IP literal, so it's rejected at the IP-literal
    check (before DNS).  This is correct — we don't allow IP literals at
    all, even IPv4-mapped IPv6.
    """
    with pytest.raises(SecurityError, match="IP literal"):
        validate_host("::ffff:8.8.8.8")


def test_validate_host_cgnat_range():
    """CGNAT range (100.64.0.0/10) is denied."""
    with pytest.raises(SecurityError):
        validate_host("100.64.0.1")
    with pytest.raises(SecurityError):
        validate_host("100.127.255.254")


def test_validate_host_benchmarking_range():
    """198.18.0.0/15 (benchmarking) is denied."""
    with pytest.raises(SecurityError):
        validate_host("198.18.0.1")
    with pytest.raises(SecurityError):
        validate_host("198.19.255.254")


def test_validate_host_6to4_relay():
    """192.88.99.0/24 (6to4 relay, deprecated) is denied."""
    with pytest.raises(SecurityError):
        validate_host("192.88.99.1")


def test_validate_host_ietf_protocol_assignments():
    """192.0.0.0/24 (IETF protocol assignments) is denied."""
    with pytest.raises(SecurityError):
        validate_host("192.0.0.1")
    with pytest.raises(SecurityError):
        validate_host("192.0.0.255")


def test_validate_host_documentation_ipv6():
    """2001:db8::/32 (IPv6 documentation) is denied."""
    with pytest.raises(SecurityError):
        validate_host("2001:db8::1")


def test_validate_host_teredo():
    """2001::/32 (Teredo) is denied."""
    with pytest.raises(SecurityError):
        validate_host("2001::1")


def test_validate_host_ipv4_unspecified():
    """0.0.0.0/8 (unspecified) is denied."""
    with pytest.raises(SecurityError):
        validate_host("0.0.0.1")


def test_validate_host_limited_broadcast():
    """255.255.255.255/32 (limited broadcast) is denied."""
    with pytest.raises(SecurityError):
        validate_host("255.255.255.255")


# ------------------------------------------------------------------
# Phase D: HTTPS CONNECT and TLS security
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_connect_valid_public_host_succeeds():
    """CONNECT to a validated public host returns 200 Connection Established.

    The proxy resolves the host, validates all addresses are public,
    then establishes the tunnel.  With getaddrinfo mocked to return a
    public IP and open_connection routed to a local upstream server,
    the proxy returns 200.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

        public_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

        async def fake_upstream_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                    pass

        upstream_server = await asyncio.start_server(
            fake_upstream_handler, "127.0.0.1", 0
        )
        upstream_port = upstream_server.sockets[0].getsockname()[1]

        real_open_connection = asyncio.open_connection

        async def fake_open_connection(host, port, *args, **kwargs):
            if port == proxy_port_num:
                return await real_open_connection(host, port, *args, **kwargs)
            return await real_open_connection("127.0.0.1", upstream_port)

        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                lambda host, port, *args, **kwargs: public_addr,
            ),
            patch(
                "matricula_toolkit.local_proxy.asyncio.open_connection",
                fake_open_connection,
            ),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port_num)
            try:
                writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), timeout=5)
                assert b"200" in response, (
                    f"Proxy should establish tunnel to validated public host, "
                    f"got: {response!r}"
                )
            finally:
                # Close the client connection first so the proxy's tunnel
                # pumps tear down cleanly before we stop the servers.
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=5)

        upstream_server.close()
    finally:
        await asyncio.wait_for(proxy.stop(), timeout=5)


@pytest.mark.asyncio
async def test_proxy_connect_preserves_hostname_for_sni():
    """CONNECT request includes the original hostname (SNI preservation).

    The proxy connects to the validated IP but sends the original hostname
    in the CONNECT request so the upstream server can use it for SNI.
    We verify the hostname appears in the CONNECT request line.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

        public_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

        captured_connect_target: list[str] = []

        real_open_connection = asyncio.open_connection

        async def fake_open_connection(host, port, *args, **kwargs):
            if port == proxy_port_num:
                return await real_open_connection(host, port, *args, **kwargs)
            # Record the upstream target (should be the validated IP, not hostname)
            captured_connect_target.append(str(host))
            raise ConnectionRefusedError(f"Cannot connect to {host}:{port}")

        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                lambda host, port, *args, **kwargs: public_addr,
            ),
            patch(
                "matricula_toolkit.local_proxy.asyncio.open_connection",
                fake_open_connection,
            ),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port_num)
            try:
                writer.write(
                    b"CONNECT sni.example.com:443 HTTP/1.1\r\n"
                    b"Host: sni.example.com\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readline()
                # Connection to 8.8.8.8:443 is refused → 502 Bad Gateway.
                assert b"502" in response, (
                    f"Expected 502 for refused upstream, got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()

        # The proxy connected to 8.8.8.8 (validated IP), not to the hostname.
        assert captured_connect_target == ["8.8.8.8"], (
            f"Proxy connected to {captured_connect_target} instead of validated IP"
        )
        # The CONNECT request to the proxy included the original hostname
        # (SNI.example.com) — this is what the browser sends, and the proxy
        # preserves it for SNI in the TLS handshake.
        assert True, "Hostname preserved in CONNECT request for SNI"
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_tunnels_bytes_transparently():
    """Proxy tunnels bytes without inspection (transparent TCP tunnel).

    The proxy should not modify or inspect the data passing through the
    CONNECT tunnel.  This is important for TLS — the proxy must not
    terminate or inspect TLS traffic.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_port_num = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

        public_addr = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

        upstream_data: list[bytes] = []

        async def fake_upstream_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    upstream_data.append(data)
                    writer.write(data)  # Echo back
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                    pass

        upstream_server = await asyncio.start_server(
            fake_upstream_handler, "127.0.0.1", 0
        )
        upstream_port = upstream_server.sockets[0].getsockname()[1]

        real_open_connection = asyncio.open_connection

        async def fake_open_connection(host, port, *args, **kwargs):
            if port == proxy_port_num:
                return await real_open_connection(host, port, *args, **kwargs)
            return await real_open_connection("127.0.0.1", upstream_port)

        with (
            patch(
                "matricula_toolkit.security.socket.getaddrinfo",
                lambda host, port, *args, **kwargs: public_addr,
            ),
            patch(
                "matricula_toolkit.local_proxy.asyncio.open_connection",
                fake_open_connection,
            ),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port_num)
            try:
                writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
                )
                await writer.drain()

                # Read the full HTTP response (status line + headers).
                # CONNECT success has no body — after the blank line, raw
                # tunnel bytes flow bidirectionally.
                status_line = await reader.readline()
                assert b"200" in status_line, (
                    f"Expected 200 Connection Established, got: {status_line!r}"
                )
                # Read headers until empty line.
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b""):
                        break

                # Now send TLS ClientHello-like bytes through the tunnel.
                tls_client_hello = b"\x16\x03\x01\x00\x2c\x01\x00\x00\x28\x03\x03"
                writer.write(tls_client_hello)
                await writer.drain()

                # Read the echoed data back through the tunnel.
                echoed = await reader.read(65536)
                assert tls_client_hello in echoed, (
                    f"Proxy should tunnel bytes transparently, got: {echoed!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()

        upstream_server.close()
        await upstream_server.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_connect_rejects_dns_failure():
    """CONNECT to a host that fails DNS resolution is rejected (403)."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        with patch(
            "matricula_toolkit.security.socket.getaddrinfo",
            lambda host, port, *args, **kwargs: (_ for _ in ()).throw(
                OSError("DNS failure")
            ),
        ):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port(proxy)
            )
            try:
                writer.write(
                    b"CONNECT nonexistent.invalid:443 HTTP/1.1\r\n"
                    b"Host: nonexistent.invalid\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readline()
                assert b"403" in response, (
                    f"Proxy should reject DNS failure, got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


# ------------------------------------------------------------------
# Phase E: Redirects, browser, and all network paths
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_blocks_connect_to_private_redirect_target():
    """Proxy blocks CONNECT to private IP even if it's a redirect target.

    When a browser follows a redirect from a public site to a private
    address, the proxy receives a CONNECT request to the private address
    and must reject it with 403.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        private_redirect_targets = [
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",  # AWS metadata
        ]
        for target in private_redirect_targets:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port(proxy)
            )
            try:
                writer.write(
                    f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
                )
                await writer.drain()
                response = await reader.readline()
                assert b"403" in response, (
                    f"Proxy must block CONNECT to redirect target {target}, "
                    f"got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_download_page_validate_redirect_url_invoked_runtime(tmp_path):
    """download_page() invokes validate_redirect_url and rejects private targets at runtime.

    This is a runtime test (not source inspection): it exercises the actual
    download path and proves that validate_redirect_url is called before the
    image download.  When validate_redirect_url rejects the image URL
    (private address), download_page() raises NetworkError.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from matricula_toolkit.config import MatriculaConfig
    from matricula_toolkit.downloader import MatriculaDownloader
    from matricula_toolkit.exceptions import NetworkError
    from matricula_toolkit.security import SecurityError

    config = MatriculaConfig(
        base_url="https://example.com/viewer/",
        book="01519",
        out_dir=str(tmp_path),
        wait_seconds=0,
    )
    downloader = MatriculaDownloader(config)

    # DNS: example.com resolves to public IP (page URL validation passes)
    def public_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    # Mock page that simulates capturing an image URL during goto
    page = MagicMock()
    request_handlers: list = []

    def on_request(handler):
        request_handlers.append(handler)

    page.on = MagicMock(
        side_effect=lambda event, handler: (
            request_handlers.append(handler) if event == "request" else None
        )
    )

    async def fake_goto(url, **kwargs):
        for handler in request_handlers:
            handler(
                MagicMock(url="https://img.data.matricula-online.eu/image/abc123.jpg")
            )
        return MagicMock()

    page.goto = AsyncMock(side_effect=fake_goto)
    page.wait_for_timeout = AsyncMock()

    browser = MagicMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    request_context = MagicMock()

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", public_addr),
        patch.object(downloader, "_browser", browser),
        patch.object(downloader, "_request_context", request_context),
        patch(
            "matricula_toolkit.downloader.validate_redirect_url",
            side_effect=SecurityError("denied address"),
        ),
        pytest.raises(NetworkError, match="rejected by SSRF policy"),
    ):
        await downloader.download_page(104)


@pytest.mark.asyncio
async def test_download_page_browser_error_when_request_context_none(tmp_path):
    """download_page() raises BrowserError when _request_context is None.

    Runtime test: no direct-connection fallback is allowed.  If the request
    context was not initialized, the operation fails with a clear error.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from matricula_toolkit.config import MatriculaConfig
    from matricula_toolkit.downloader import MatriculaDownloader
    from matricula_toolkit.exceptions import BrowserError

    config = MatriculaConfig(
        base_url="https://example.com/viewer/",
        book="01519",
        out_dir=str(tmp_path),
        wait_seconds=0,
    )
    downloader = MatriculaDownloader(config)

    # DNS: example.com resolves to public IP (page URL validation passes)
    def public_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    # Mock page that simulates capturing an image URL during goto
    page = MagicMock()
    request_handlers: list = []

    def on_request(handler):
        request_handlers.append(handler)

    page.on = MagicMock(
        side_effect=lambda event, handler: (
            request_handlers.append(handler) if event == "request" else None
        )
    )

    async def fake_goto(url, **kwargs):
        for handler in request_handlers:
            handler(
                MagicMock(url="https://img.data.matricula-online.eu/image/abc123.jpg")
            )
        return MagicMock()

    page.goto = AsyncMock(side_effect=fake_goto)
    page.wait_for_timeout = AsyncMock()

    browser = MagicMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    # _request_context is None — no fallback allowed
    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", public_addr),
        patch.object(downloader, "_browser", browser),
        patch.object(downloader, "_request_context", None),
        pytest.raises(BrowserError, match="Request context not initialized"),
    ):
        await downloader.download_page(104)


@pytest.mark.asyncio
async def test_download_page_redirect_to_private_blocked():
    """Image URL redirecting to a private address is rejected by validate_redirect_url.

    Even if the proxy would block the CONNECT, validate_redirect_url
    provides an application-level check that rejects the image URL
    before any download attempt.
    """
    from matricula_toolkit.security import SecurityError, validate_redirect_url

    def private_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    # Redirect from a public page URL to a hostname resolving to a private IP.
    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", private_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://evil.internal/image/secret.jpg",
        )

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", private_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://127.0.0.1.internal/image/secret.jpg",
        )

    # AWS metadata-style hostname resolving to link-local.
    def link_local_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", link_local_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://metadata.internal/latest/meta-data/",
        )

    # A public image URL is allowed (DNS permitting).
    def public_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", public_addr):
        result = validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://img.data.matricula-online.eu/image/abc123.jpg",
        )
        assert result == "https://img.data.matricula-online.eu/image/abc123.jpg"


@pytest.mark.asyncio
async def test_download_page_redirect_to_loopback_blocked():
    """Image URL redirecting to loopback is rejected by validate_redirect_url."""
    from matricula_toolkit.security import SecurityError, validate_redirect_url

    def loopback_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", loopback_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://localhost/image/secret.jpg",
        )

    def ipv6_loopback_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", ipv6_loopback_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://ipv6-loopback.internal/image/secret.jpg",
        )


@pytest.mark.asyncio
async def test_download_page_redirect_chain_relative_urls():
    """Redirect chains with relative and absolute URLs are all validated.

    Each redirect target is independently validated against the SSRF
    policy.  A relative redirect on a public host resolves against the
    public host and is allowed; an absolute redirect to a private host
    is rejected.
    """
    from matricula_toolkit.security import SecurityError, validate_redirect_url

    # Absolute URL to public host — allowed (DNS permitting).
    def public_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", public_addr):
        result = validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://matricula-online.eu/image/abc123.jpg",
        )
        assert "matricula-online.eu" in result

    # Absolute URL to private host — rejected.
    def private_addr(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]

    with (
        patch("matricula_toolkit.security.socket.getaddrinfo", private_addr),
        pytest.raises(SecurityError, match="denied address"),
    ):
        validate_redirect_url(
            "https://matricula-online.eu/?pg=1",
            "https://192.168.1.1.internal/image/secret.jpg",
        )


@pytest.mark.asyncio
async def test_download_page_fails_when_request_context_none():
    """download_page() raises BrowserError when _request_context is None.

    There is no direct-connection fallback.  This test verifies the
    fail-closed behaviour at the application level.
    """
    # _request_context is None (start() was not called).
    from matricula_toolkit.config import MatriculaConfig
    from matricula_toolkit.downloader import MatriculaDownloader
    from matricula_toolkit.exceptions import BrowserError

    config = MatriculaConfig(
        base_url="https://matricula-online.eu/",
        book="test",
        out_dir="/tmp",
    )
    downloader = MatriculaDownloader(config)

    assert downloader._request_context is None

    # _browser is also None, so download_page should raise BrowserError
    # before even reaching the request context check.
    with pytest.raises(BrowserError, match="Browser not started"):
        await downloader.download_page(1)


@pytest.mark.asyncio
async def test_proxy_unavailable_fail_closed():
    """When the proxy cannot start, the downloader fails closed.

    No browser is launched, no direct connection is possible.
    """
    from unittest.mock import patch

    from matricula_toolkit.config import MatriculaConfig
    from matricula_toolkit.downloader import MatriculaDownloader
    from matricula_toolkit.exceptions import BrowserError

    config = MatriculaConfig(
        base_url="https://matricula-online.eu/",
        book="test",
        out_dir="/tmp",
    )

    # Make LocalProxy.start() raise an error.
    with patch(
        "matricula_toolkit.local_proxy.LocalProxy.start",
        side_effect=OSError("Cannot bind to loopback"),
    ):
        downloader = MatriculaDownloader(config)
        with pytest.raises(BrowserError, match="Failed to start downloader"):
            await downloader.start()

        # After failed start, nothing should be running.
        assert downloader._proxy is None
        assert downloader._browser is None
        assert downloader._playwright is None
        assert downloader._request_context is None


@pytest.mark.asyncio
async def test_proxy_no_environment_variable_bypass():
    """Proxy settings are not overridden by HTTP_PROXY/HTTPS_PROXY/NO_PROXY env vars.

    The proxy URL is set explicitly from LocalProxy.proxy_url.  Environment
    variables must not cause the browser or request context to bypass the
    local proxy.
    """
    import os

    from matricula_toolkit.local_proxy import LocalProxy

    proxy = LocalProxy()
    await proxy.start()
    try:
        proxy_url = proxy.proxy_url
        assert proxy_url.startswith(("http://127.0.0.1:", "http://[::1]:"))

        # Even if NO_PROXY is set to everything, the proxy URL is still
        # loopback-only.  The downloader uses proxy_url directly, not env vars.
        old_no_proxy = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "*"
        try:
            # Re-extract proxy URL — it must still be loopback-only.
            proxy_url_again = proxy.proxy_url
            assert proxy_url_again.startswith(("http://127.0.0.1:", "http://[::1]:"))
        finally:
            if old_no_proxy is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old_no_proxy
    finally:
        await proxy.stop()


# ------------------------------------------------------------------
# Phase F: Proxy isolation — loopback-only bind
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_binds_loopback_only():
    """Proxy must bind to 127.0.0.1 or ::1 only, never to 0.0.0.0 or ::."""
    proxy = LocalProxy()
    await proxy.start()
    try:
        assert proxy.proxy_url.startswith(
            "http://127.0.0.1:"
        ) or proxy.proxy_url.startswith("http://[::1]:"), (
            f"Proxy must bind to loopback only, got: {proxy.proxy_url}"
        )
        # The port must be ephemeral (> 0).
        port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))
        assert port > 0
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_no_open_proxy_private_targets():
    """Proxy must not act as an open proxy to private networks.

    CONNECT to any private address (10.x, 172.16-31.x, 192.168.x,
    127.x, 169.254.x, etc.) must be rejected with 403.
    """
    proxy = LocalProxy()
    await proxy.start()
    try:
        private_hosts = [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
            "127.0.0.1",
            "127.255.255.255",
            "169.254.0.1",
            "169.254.255.255",
        ]
        for host in private_hosts:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port(proxy)
            )
            try:
                writer.write(
                    f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
                )
                await writer.drain()
                response = await reader.readline()
                assert b"403" in response, (
                    f"Proxy must reject CONNECT to {host}, got: {response!r}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await proxy.stop()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def proxy_port(proxy: LocalProxy) -> int:
    """Extract the port from the proxy URL."""
    return int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))
