"""Loopback-only asyncio HTTP/CONNECT proxy with SSRF protection.

This proxy is the single egress point for all outbound network traffic from
the Matricula Toolkit.  It implements:

* Loopback-only bind (127.0.0.1 and ::1), ephemeral port.
* DNS resolution performed by the proxy (not the client/browser).
* Every target validated through :mod:`matricula_toolkit.security` before
  any connection is made (fail-closed).
* DNS-rebinding protection: all resolved addresses checked; mixed
  public/private results rejected; connection made to a validated IP.
* CONNECT method for HTTPS (tunnel to validated IP, preserving SNI).
* No open proxy: only validated targets are allowed.
* Consistent IPv4/IPv6 handling.

The proxy is started per-downloader-session and stopped when the session
ends.  If the proxy cannot start or a request fails validation, the caller
must fail closed — never fall back to a direct connection.
"""

from __future__ import annotations

import asyncio
import ipaddress

from matricula_toolkit.exceptions import MatriculaError
from matricula_toolkit.security import (
    SecurityError,
    _resolve_host,
    _validate_addresses,
    validate_url,
)

# Maximum time (seconds) to establish the upstream TCP connection after
# a CONNECT target has been validated.  Prevents a slow or non-responsive
# public host from holding a proxy tunnel handler indefinitely.
CONNECT_TIMEOUT_SECONDS = 30

# Maximum time (seconds) to wait for data on a tunnel pump (read or drain).
# A stalled tunnel must not hold handler tasks or sockets indefinitely.
TUNNEL_IO_TIMEOUT_SECONDS = 60


class ProxyError(MatriculaError):
    """Raised when the local proxy cannot operate."""


class LocalProxy:
    """A loopback-only HTTP/CONNECT proxy with SSRF protection.

    The proxy binds to loopback interfaces only and uses an ephemeral port
    assigned by the OS.  It resolves DNS itself, validates every target
    through the centralized security policy, and connects to a validated
    public IP.  TLS/SNI is preserved by connecting to the validated IP while
    sending the original hostname in the TLS handshake (via the CONNECT
    tunnel for HTTPS).
    """

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._host: str = "127.0.0.1"
        self._port: int = 0
        self._started: bool = False
        self._client_tasks: set[asyncio.Task] = set()

    @property
    def proxy_url(self) -> str:
        """Return the proxy URL (http://127.0.0.1:port) or empty if not started."""
        if not self._started or self._port == 0:
            return ""
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        """Start the proxy server on a loopback interface with an ephemeral port."""
        if self._started:
            return

        # Try IPv4 loopback first, fall back to IPv6 loopback.
        for host in ("127.0.0.1", "::1"):
            try:
                self._server = await asyncio.start_server(self._handle_client, host, 0)
                self._host = host
                break
            except OSError:
                continue
        else:
            raise ProxyError("Could not bind proxy to any loopback interface")

        # Determine the assigned ephemeral port.
        sockets = self._server.sockets  # type: ignore[attr-defined]
        if sockets:
            sock = sockets[0]
            self._port = sock.getsockname()[1]
        self._started = True

    async def stop(self) -> None:
        """Stop the proxy server and all active client handler tasks.

        Closes the listening server, cancels any active CONNECT tunnel
        handler tasks, and waits for the server to fully close.  This
        prevents ``wait_closed()`` from hanging indefinitely when active
        tunnels are blocked on socket reads that will never produce data
        (e.g. after the client has closed its socket).
        """
        # Cancel all active client handler tasks first so that wait_closed()
        # does not hang waiting for tunnels that are blocked on reads.
        if self._client_tasks:
            for task in self._client_tasks:
                task.cancel()
            # Wait for all cancelled tasks to finish.
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
            self._client_tasks.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._started = False
        self._port = 0

    # ------------------------------------------------------------------
    # Client handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection (HTTP request or CONNECT tunnel)."""
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
            try:
                await self._run_client_handler(reader, writer)
            finally:
                self._client_tasks.discard(task)
        else:
            await self._run_client_handler(reader, writer)

    async def _run_client_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection (HTTP request or CONNECT tunnel)."""
        try:
            # Read the request line.
            request_line_bytes = await reader.readline()
            if not request_line_bytes:
                return
            request_line = request_line_bytes.decode("utf-8", errors="replace").strip()
            parts = request_line.split()
            if len(parts) < 2:
                self._send_response(writer, 400, "Bad Request")
                return

            method = parts[0].upper()
            target = parts[1]

            if method == "CONNECT":
                await self._handle_connect(reader, writer, target)
            else:
                await self._handle_http(reader, writer, method, target)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            # Task was cancelled during proxy shutdown — clean up and re-raise.
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                pass
            raise
        except Exception:  # noqa: BLE001 — never leak internal details to client
            try:
                self._send_response(writer, 502, "Bad Gateway")
            except Exception:  # noqa: S110,BLE001 — never leak internal details to client
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                pass

    # ------------------------------------------------------------------
    # CONNECT (HTTPS tunneling)
    # ------------------------------------------------------------------

    async def _handle_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, authority: str
    ) -> None:
        """Handle a CONNECT request: tunnel to a validated target host:port."""
        # Parse host:port from authority.
        if ":" in authority:
            host, _, port_str = authority.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                self._send_response(writer, 400, "Bad Request")
                return
        else:
            host = authority
            port = 443

        # Only port 443 is allowed.
        if port != 443:
            self._send_response(writer, 403, "Forbidden: only port 443 allowed")
            return

        # Basic host checks (empty, IP literal) — same as validate_host.
        if not host or not host.strip():
            self._send_response(writer, 403, "Forbidden: Host is empty")
            return
        try:
            ipaddress.ip_address(host)
            self._send_response(
                writer, 403, "Forbidden: IP literal hosts are not allowed"
            )
            return
        except ValueError:
            pass

        # IDNA-encode internationalized domain names to ASCII.
        # The encoded host is used for DNS resolution and preserved for
        # TLS/SNI (the original hostname is already in the CONNECT request).
        try:
            validated_host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError) as exc:
            self._send_response(
                writer, 403, f"Forbidden: Invalid hostname {host!r}: {exc}"
            )
            return

        # Consume the remaining HTTP request headers so they don't leak
        # into the CONNECT tunnel as client data.
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break

        # Resolve DNS exactly once and validate all addresses.
        # The same resolved address list is used for both validation and
        # the actual connection, eliminating the TOCTOU window where DNS
        # could return different IPs between validation and connect.
        try:
            addresses = _resolve_host(validated_host)
            _validate_addresses(validated_host, addresses)
        except SecurityError as exc:
            self._send_response(writer, 403, f"Forbidden: {exc}")
            return

        # Pick the first validated public address (IPv4 preferred).
        # All addresses are guaranteed public by _validate_addresses above.
        target_addr = None
        for addr in addresses:
            if isinstance(addr, ipaddress.IPv4Address):
                target_addr = str(addr)
                break
        if target_addr is None:
            target_addr = str(addresses[0])

        # Open a TCP connection to the validated IP with a timeout so a
        # slow or non-responsive host cannot hold the handler indefinitely.
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(target_addr, port),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self._send_response(writer, 502, f"Bad Gateway: {exc}")
            return

        # Tell the client the tunnel is established.
        # CONNECT success must have no body — after the blank line, raw
        # tunnel bytes flow bidirectionally.  Sending Content-Length or
        # Connection: close would break the tunnel.
        connect_response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        writer.write(connect_response)
        try:
            await asyncio.wait_for(writer.drain(), timeout=TUNNEL_IO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # Client not reading the CONNECT response — close and give up.
            writer.close()
            await writer.wait_closed()
            return

        # Proxy bytes bidirectionally between client and upstream.
        await self._proxy_tunnel(reader, writer, upstream_reader, upstream_writer)

    def _select_target_address(self, host: str) -> str:
        """Resolve *host* and return a validated public IP address string.

        .. deprecated:: 0.2.0
            Use :meth:`_handle_connect`'s inline resolution instead.
            This method re-resolves DNS and is retained only for
            backward compatibility with existing tests.  New code should
            resolve once and reuse the result (see F1 fix).

        Uses the same DNS resolution and deny-list logic as
        :func:`matricula_toolkit.security.validate_host` to avoid TOCTOU
        and inconsistent-validation issues.  All resolved addresses must be
        public; if any is denied, the host is rejected (fail-closed).

        Raises :class:`SecurityError` if no valid public address exists.
        """
        # Reuse the same resolution + validation as validate_host so there
        # is no TOCTOU window between validation and connection.
        addresses = _resolve_host(host)
        _validate_addresses(host, addresses)

        # Return the first public address (all are guaranteed public by
        # _validate_addresses above).  Prefer IPv4 when available.
        for addr in addresses:
            if isinstance(addr, ipaddress.IPv4Address):
                return str(addr)
        # Fall back to the first address (IPv6).
        return str(addresses[0])

    # ------------------------------------------------------------------
    # HTTP (plain, not used for our https-only policy but supported)
    # ------------------------------------------------------------------

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
    ) -> None:
        """Handle a non-CONNECT HTTP request (forwarded to validated target)."""
        try:
            validate_url(target)
        except SecurityError as exc:
            self._send_response(writer, 403, f"Forbidden: {exc}")
            return

        # For our https-only policy, plain HTTP forward is not expected.
        # Reject to keep the proxy strictly scoped.
        self._send_response(writer, 405, "Method Not Allowed: use CONNECT for https")

    # ------------------------------------------------------------------
    # Tunnel + helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _proxy_tunnel(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Proxy bytes bidirectionally until one side closes.

        Each pump operation (read + drain) is bounded by
        :data:`TUNNEL_IO_TIMEOUT_SECONDS` so a stalled tunnel cannot hold
        handler tasks or sockets indefinitely.  On timeout, the affected
        direction is closed and the other direction is allowed to finish
        naturally.
        """

        async def pump(
            src: asyncio.StreamReader,
            dst: asyncio.StreamWriter,
            name: str = "",
        ) -> None:
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(
                            src.read(65536), timeout=TUNNEL_IO_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # Read stalled — close this direction and stop.
                        break
                    if not data:
                        break
                    dst.write(data)
                    try:
                        await asyncio.wait_for(
                            dst.drain(), timeout=TUNNEL_IO_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # Drain stalled — close this direction and stop.
                        break
            except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                    pass

        await asyncio.gather(
            pump(client_reader, upstream_writer, "C->U"),
            pump(upstream_reader, client_writer, "U->C"),
            return_exceptions=True,
        )

    @staticmethod
    def _send_response(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        """Send a minimal HTTP response to the client."""
        body = f"{reason}\r\n".encode()
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(response)
