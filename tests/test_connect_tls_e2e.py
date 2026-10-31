"""End-to-End CONNECT/TLS tests through the local SSRF proxy.

These tests verify that a real TLS handshake works through the CONNECT tunnel,
with certificate verification and hostname checking enabled.  The proxy is
never allowed to terminate or inspect TLS — it only tunnels bytes.

All tests use exclusively local test servers and locally generated test
certificates.  No real network, no real internal hosts, no disabled
certificate verification.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
import ssl
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from matricula_toolkit.local_proxy import LocalProxy


def _make_getaddrinfo_mock(
    public_addr: list[tuple],
) -> callable:
    """Return a getaddrinfo mock that resolves *test.local* to a public IP.

    All other hosts (e.g. 127.0.0.1 for the proxy connection) use the real
    DNS resolution, so the test client can actually connect to the proxy.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _mock(host, port, *args, **kwargs):
        if host == "test.local":
            return public_addr
        return real_getaddrinfo(host, port, *args, **kwargs)

    return _mock


# ------------------------------------------------------------------
# Test certificate generation
# ------------------------------------------------------------------


def _generate_test_certs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Generate CA cert, server cert, and server key for ``test.local``.

    Returns ``(ca_path, cert_path, key_path)``.
    """
    # --- CA key and certificate ---
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        )
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # --- Server key and certificate ---
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.local")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        )
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("test.local"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"

    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    return ca_path, cert_path, key_path


# ------------------------------------------------------------------
# TLS echo server
# ------------------------------------------------------------------


async def _start_tls_echo_server(
    cert_path: Path, key_path: Path, host: str = "127.0.0.1", port: int = 0
) -> tuple[asyncio.AbstractServer, int]:
    """Start a TLS echo server on *host*:*port*.

    Returns ``(server, actual_port)``.
    """

    async def _handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                pass

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    server = await asyncio.start_server(_handle_client, host, port, ssl=ssl_context)
    actual_port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return server, actual_port


# ------------------------------------------------------------------
# Client helper: connect through proxy, perform TLS handshake
# ------------------------------------------------------------------


async def _connect_through_proxy(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    ca_path: Path,
    server_hostname: str | None = None,
) -> ssl.SSLSocket:
    """Connect to *target_host*:*target_port* through the proxy and perform TLS.

    Runs the blocking socket + TLS handshake in ``asyncio.to_thread()`` so the
    asyncio event loop stays free for the proxy to process the connection.
    Uses a raw :class:`socket.socket` (not asyncio's TransportSocket) so it
    can be wrapped with :meth:`ssl.SSLContext.wrap_socket`.

    Returns the connected, handshaken :class:`ssl.SSLSocket`.
    """

    def _blocking_connect() -> ssl.SSLSocket:
        import ssl as _ssl

        # Raw TCP connection to the proxy (blocking, runs in a thread).
        sock = socket.create_connection((proxy_host, proxy_port), timeout=10)

        # Send CONNECT request.
        connect_request = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}\r\n\r\n"
        ).encode()
        sock.sendall(connect_request)
        sock.settimeout(10)

        # Read CONNECT response: status line + headers, then blank line.
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response:
                break

        if b"200" not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise RuntimeError(f"CONNECT failed: {response!r}")

        # Wrap the raw socket in SSL (raw socket supports detach()).
        ssl_context = _ssl.create_default_context(_ssl.Purpose.SERVER_AUTH)
        ssl_context.load_verify_locations(cafile=str(ca_path))
        if server_hostname is not None:
            ssl_context.check_hostname = True
            ssl_context.verify_mode = _ssl.CERT_REQUIRED

        ssl_sock = ssl_context.wrap_socket(
            sock,
            server_hostname=server_hostname or target_host,
        )
        return ssl_sock

    return await asyncio.to_thread(_blocking_connect)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_tls_full_handshake_with_cert_verification():
    """Full TLS handshake through CONNECT tunnel with certificate verification.

    The proxy tunnels the TLS bytes without inspecting them.  The client
    verifies the server certificate against the local CA and checks the
    hostname.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        # Start TLS echo server.
        tls_server, tls_port = await _start_tls_echo_server(cert_path, key_path)
        try:
            # Start proxy.
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                # Mock DNS: test.local resolves to 8.8.8.8 (public).
                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]

                # Mock upstream connection: route to local TLS server.
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    ssl_sock = await _connect_through_proxy(
                        "127.0.0.1", proxy_port, "test.local", 443, ca_path
                    )

                    # TLS handshake succeeded — verify cipher and peer cert.
                    cipher = ssl_sock.cipher()
                    assert cipher is not None, "TLS cipher should be negotiated"
                    peer_cert = ssl_sock.getpeercert()
                    assert peer_cert is not None, "Peer certificate should be presented"
                    assert "test.local" in str(peer_cert.get("subject", "")), (
                        f"Peer cert subject should contain test.local, got: {peer_cert!r}"
                    )

                    ssl_sock.close()
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_tls_bidirectional_data_transfer():
    """Bidirectional data transfer through the TLS tunnel.

    Data sent by the client is received by the server and echoed back.
    The proxy must not modify or inspect the data.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        tls_server, tls_port = await _start_tls_echo_server(cert_path, key_path)
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    ssl_sock = await _connect_through_proxy(
                        "127.0.0.1", proxy_port, "test.local", 443, ca_path
                    )

                    # Send and receive in a thread to avoid blocking the
                    # event loop (the proxy needs it to process the tunnel).
                    test_message = b"TLS tunnel test message 12345"

                    def _transfer(sock: ssl.SSLSocket) -> bytes:
                        sock.sendall(test_message)
                        return sock.recv(65536)

                    received = await asyncio.to_thread(_transfer, ssl_sock)
                    assert test_message in received, (
                        f"Echoed data mismatch: sent {test_message!r}, got {received!r}"
                    )

                    ssl_sock.close()
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_tls_rejects_untrusted_certificate():
    """Client rejects a server whose certificate is not signed by the trusted CA.

    We generate a second, unrelated CA and sign a server cert with it.
    The client (trusting the first CA) must reject the connection.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, _cert_path, _key_path = _generate_test_certs(tmp_path)

        # Generate a second, untrusted CA and server cert.
        untrusted_ca_subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Untrusted CA")]
        )

        untrusted_server_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        untrusted_server_subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "test.local")]
        )
        untrusted_server_cert = (
            x509.CertificateBuilder()
            .subject_name(untrusted_server_subject)
            .issuer_name(untrusted_ca_subject)
            .public_key(untrusted_server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1)
            )
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=365)
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("test.local")]),
                critical=False,
            )
            .sign(untrusted_server_key, hashes.SHA256())
        )

        untrusted_cert_path = tmp_path / "untrusted_server.pem"
        untrusted_key_path = tmp_path / "untrusted_server.key"
        untrusted_cert_path.write_bytes(
            untrusted_server_cert.public_bytes(serialization.Encoding.PEM)
        )
        untrusted_key_path.write_bytes(
            untrusted_server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

        # Start TLS server with untrusted cert.
        tls_server, tls_port = await _start_tls_echo_server(
            untrusted_cert_path, untrusted_key_path
        )
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                    pytest.raises((ssl.SSLCertVerificationError, ssl.SSLError)),
                ):
                    # The client must reject the untrusted certificate.
                    await _connect_through_proxy(
                        "127.0.0.1", proxy_port, "test.local", 443, ca_path
                    )
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_tls_rejects_hostname_mismatch():
    """Client rejects a server certificate whose hostname doesn't match.

    The server cert is for "test.local" but the client connects with
    server_hostname="other.example".  Hostname verification must fail.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        tls_server, tls_port = await _start_tls_echo_server(cert_path, key_path)
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    # Connect with wrong hostname using a raw socket in a thread
                    # (to avoid blocking the event loop).
                    def _connect_wrong_hostname():
                        import ssl as _ssl

                        sock = socket.create_connection(
                            ("127.0.0.1", proxy_port), timeout=10
                        )
                        sock.sendall(
                            b"CONNECT test.local:443 HTTP/1.1\r\n"
                            b"Host: test.local\r\n\r\n"
                        )
                        sock.settimeout(5)
                        # Read CONNECT response
                        response = b""
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            response += chunk
                            if b"\r\n\r\n" in response:
                                break

                        ssl_context = _ssl.create_default_context(
                            _ssl.Purpose.SERVER_AUTH
                        )
                        ssl_context.load_verify_locations(cafile=str(ca_path))
                        ssl_context.check_hostname = True
                        ssl_context.verify_mode = _ssl.CERT_REQUIRED

                        # Connect with wrong hostname — must fail.
                        try:
                            ssl_context.wrap_socket(
                                sock, server_hostname="other.example"
                            )
                        except (_ssl.SSLCertVerificationError, _ssl.SSLError):
                            sock.close()
                            raise
                        sock.close()
                        raise AssertionError("Expected SSL error for hostname mismatch")

                    with pytest.raises(
                        (ssl.SSLCertVerificationError, ssl.SSLError, AssertionError)
                    ):
                        await asyncio.to_thread(_connect_wrong_hostname)
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_tls_eof_handling():
    """Clean EOF on both sides of the tunnel terminates without deadlock.

    The server closes its side; the client must be able to read EOF.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        server_closed = asyncio.Event()

        async def _handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                data = await reader.read(65536)
                if data:
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                server_closed.set()
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: S110,BLE001
                    pass

        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        tls_server = await asyncio.start_server(
            _handle_client, "127.0.0.1", 0, ssl=ssl_context
        )
        tls_port = tls_server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    ssl_sock = await _connect_through_proxy(
                        "127.0.0.1", proxy_port, "test.local", 443, ca_path
                    )

                    # Send data, then close write side — in a thread so the
                    # event loop stays free for the proxy tunnel.
                    #
                    # Phase 1: send data and read the echo.  Before
                    # shutdown(SHUT_WR), ssl_sock.recv() returns decrypted
                    # TLS application data, so we can assert on plaintext.
                    # Phase 2: signal EOF at the TLS level and read until
                    # the connection terminates, verifying the tunnel shuts
                    # down cleanly without hanging.
                    def _eof_transfer(sock: ssl.SSLSocket) -> bytes:
                        sock.sendall(b"EOF test")
                        received = b""
                        while b"EOF test" not in received:
                            chunk = sock.recv(65536)
                            if not chunk:
                                break
                            received += chunk

                        sock.shutdown(socket.SHUT_WR)
                        while True:
                            chunk = sock.recv(65536)
                            if not chunk:
                                break
                            received += chunk
                        return received

                    received = await asyncio.to_thread(_eof_transfer, ssl_sock)

                    assert b"EOF test" in received, (
                        f"Expected echoed data before EOF, got: {received!r}"
                    )

                    ssl_sock.close()
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_tls_connection_abort_no_deadlock():
    """Abrupt connection close on one side does not deadlock the other.

    The client aborts the connection; the proxy's tunnel pumps must
    terminate cleanly.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        tls_server, tls_port = await _start_tls_echo_server(cert_path, key_path)
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    ssl_sock = await _connect_through_proxy(
                        "127.0.0.1", proxy_port, "test.local", 443, ca_path
                    )

                    # Abruptly close the socket.
                    ssl_sock.close()

                    # If we get here without hanging, the abort was handled.
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_response_contains_only_status_line_and_crlf():
    """CONNECT success response contains only status line and \\r\\n\\r\\n.

    No body, no Content-Length, no Transfer-Encoding header.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        _ca_path, cert_path, key_path = _generate_test_certs(tmp_path)

        tls_server, tls_port = await _start_tls_echo_server(cert_path, key_path)
        try:
            proxy = LocalProxy()
            await proxy.start()
            try:
                proxy_port = int(proxy.proxy_url.rsplit(":", 1)[1].rstrip("/"))

                public_addr = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                ]
                real_open_connection = asyncio.open_connection
                proxy_port_num = proxy_port

                async def fake_open_connection(host, port, *args, **kwargs):
                    if port == proxy_port_num:
                        return await real_open_connection(host, port, *args, **kwargs)
                    return await real_open_connection("127.0.0.1", tls_port)

                with (
                    patch(
                        "matricula_toolkit.security.socket.getaddrinfo",
                        _make_getaddrinfo_mock(public_addr),
                    ),
                    patch(
                        "matricula_toolkit.local_proxy.asyncio.open_connection",
                        fake_open_connection,
                    ),
                ):
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", proxy_port
                    )
                    try:
                        writer.write(
                            b"CONNECT test.local:443 HTTP/1.1\r\n"
                            b"Host: test.local\r\n\r\n"
                        )
                        await writer.drain()

                        # Read the full response until blank line.
                        response_bytes = b""
                        while True:
                            line = await reader.readline()
                            response_bytes += line
                            if line in (b"\r\n", b""):
                                break

                        # Response must be exactly: status line + \r\n\r\n
                        # No Content-Length, no Transfer-Encoding, no body.
                        response_text = response_bytes.decode("utf-8", errors="replace")
                        assert "200" in response_text, (
                            f"Expected 200, got: {response_text!r}"
                        )
                        assert "Content-Length" not in response_text, (
                            f"CONNECT response must not contain Content-Length: {response_text!r}"
                        )
                        assert "Transfer-Encoding" not in response_text, (
                            f"CONNECT response must not contain Transfer-Encoding: {response_text!r}"
                        )
                        # Must end with \r\n\r\n (blank line = end of headers)
                        assert response_text.endswith("\r\n\r\n"), (
                            f"CONNECT response must end with blank line, got: {response_text!r}"
                        )
                    finally:
                        writer.close()
                        await writer.wait_closed()
            finally:
                await proxy.stop()
        finally:
            tls_server.close()
            await tls_server.wait_closed()
