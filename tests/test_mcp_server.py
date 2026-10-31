"""Mock-based tests for the MCP server.

Tests use `call_tool` to dispatch and patch `MatriculaDownloader`
at the class level so all downloader instances created by the server
are mocked.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.mcp_server import MatriculaMCPServer, validate_base_url


def _make_mock_page_result(
    success=True,
    page=1,
    file_path=None,
    file_size=1000,
    image_url=None,
    error=None,
    duration=1.0,
):
    """Create a mock PageResult."""
    r = MagicMock(spec=PageResult)
    r.success = success
    r.page_number = page
    r.file_path = file_path
    r.file_size = file_size
    r.image_url = image_url
    r.error = error
    r.duration_seconds = duration
    return r


class TestMCPServer:
    """Tests for MatriculaMCPServer using mocked downloader."""

    def test_list_tools(self):
        """The server lists all expected tools."""
        server = MatriculaMCPServer()
        tools = server.list_tools()

        tool_names = {t["name"] for t in tools}
        expected = {
            "inspect_book",
            "inspect_page",
            "download_page",
            "download_range",
            "verify_image",
            "diagnose_error",
            "prepare_ocr",
        }
        assert tool_names == expected

        # Each tool should have name, description, inputSchema
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    @pytest.mark.asyncio
    async def test_inspect_book_success(self):
        """inspect_book returns accessible info when page 1 downloads OK."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_result = _make_mock_page_result(
            success=True,
            page=1,
            file_path=MagicMock(
                __str__=MagicMock(
                    return_value="/tmp/downloads/matricula_01519_pg001.jpg"
                )
            ),
            file_size=2089000,
            image_url="https://img.data.matricula-online.eu/image/abc/?csrf=x",
        )

        with patch.object(
            MatriculaDownloader,
            "download_page",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await server.inspect_book(
                {"base_url": "https://example.com/viewer/", "book": "01519"}
            )

        assert result["accessible"] is True
        assert result["book"] == "01519"

    @pytest.mark.asyncio
    async def test_inspect_book_failure(self):
        """inspect_book reports failure when page 1 cannot be downloaded."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_result = _make_mock_page_result(
            success=False,
            page=1,
            error="ImageNotFoundError: No image URL found",
        )

        with patch.object(
            MatriculaDownloader,
            "download_page",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await server.inspect_book(
                {"base_url": "https://example.com/viewer/", "book": "01519"}
            )

        assert result["accessible"] is False
        assert "No image URL found" in result["error"]

    @pytest.mark.asyncio
    async def test_inspect_page_success(self):
        """inspect_page returns page metadata on success."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_result = _make_mock_page_result(
            success=True,
            page=104,
            file_path=MagicMock(
                __str__=MagicMock(
                    return_value="/tmp/downloads/matricula_01519_pg104.jpg"
                )
            ),
            file_size=2089000,
            image_url="https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
            duration=5.5,
        )

        with patch.object(
            MatriculaDownloader,
            "download_page",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await server.inspect_page(
                {"page": 104, "base_url": "https://example.com/viewer/"}
            )

        assert result["success"] is True
        assert result["page"] == 104
        assert result["file_size_bytes"] == 2089000

    @pytest.mark.asyncio
    async def test_download_range(self):
        """download_range returns aggregated results."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_results = [
            _make_mock_page_result(
                success=True,
                page=1,
                file_path=MagicMock(__str__=MagicMock(return_value="/tmp/p1.jpg")),
                file_size=1000,
            ),
            _make_mock_page_result(success=False, page=2, error="NetworkError"),
        ]

        with patch.object(
            MatriculaDownloader,
            "download_range",
            new=AsyncMock(return_value=mock_results),
        ):
            result = await server.download_range(
                {"start": 1, "end": 2, "base_url": "https://example.com/viewer/"}
            )

        assert result["total"] == 2
        assert result["success"] == 1
        assert result["failed"] == 1

    @pytest.mark.asyncio
    async def test_download_range_large_no_artificial_limit(self):
        """download_range processes a large page range without an artificial cap.

        No real downloads are performed; the downloader is mocked. The test
        verifies that all requested pages are passed through and none are
        silently dropped by an artificial page limit.
        """
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        # Simulate 1000 pages, all successful
        mock_results = [
            _make_mock_page_result(success=True, page=p, file_size=1000)
            for p in range(1, 1001)
        ]

        with patch.object(
            MatriculaDownloader,
            "download_range",
            new=AsyncMock(return_value=mock_results),
        ) as mock_dl:
            result = await server.download_range(
                {"start": 1, "end": 1000, "base_url": "https://example.com/viewer/"}
            )

        # All 1000 pages must be requested and processed
        assert result["total"] == 1000
        assert result["success"] == 1000
        assert result["failed"] == 0
        # Verify the downloader received the full range (no artificial cap)
        call_args = mock_dl.call_args[0]
        assert call_args[0] == 1
        assert call_args[1] == 1000

    @pytest.mark.asyncio
    async def test_verify_image_valid(self, tmp_path):
        """verify_image returns valid=True for a valid JPEG."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
        )
        server = MatriculaMCPServer(config)

        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0fake")

        with patch.object(
            MatriculaDownloader, "verify_image", new=AsyncMock(return_value=True)
        ):
            result = await server.verify_image({"path": str(img_path)})

        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_verify_image_invalid(self, tmp_path):
        """verify_image returns valid=False for an invalid file."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
        )
        server = MatriculaMCPServer(config)

        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"not a jpeg")

        with patch.object(
            MatriculaDownloader, "verify_image", new=AsyncMock(return_value=False)
        ):
            result = await server.verify_image({"path": str(img_path)})

        assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_diagnose_error(self):
        """diagnose_error returns detailed error info."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_result = _make_mock_page_result(
            success=False,
            page=104,
            error="ImageNotFoundError: No image URL found",
        )

        with patch.object(
            MatriculaDownloader,
            "download_page",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await server.diagnose_error(
                {"page": 104, "base_url": "https://example.com/viewer/"}
            )

        assert result["success"] is False
        assert "No image URL found" in result["error"]

    @pytest.mark.asyncio
    async def test_prepare_ocr(self, tmp_path):
        """prepare_ocr lists images and computes total size."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
        )
        server = MatriculaMCPServer(config)

        # Create some test images
        (tmp_path / "matricula_01519_pg001.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + b"a" * 1000
        )
        (tmp_path / "matricula_01519_pg002.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + b"b" * 2000
        )
        (tmp_path / "other.txt").write_text("ignore")

        result = await server.prepare_ocr({"directory": str(tmp_path), "book": "01519"})

        assert result["image_count"] == 2
        assert result["total_size_bytes"] == 3008  # 4 + 1000 + 4 + 2000
        assert result["book"] == "01519"

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self):
        """Calling an unknown tool returns an error."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        result = await server.call_tool("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_call_tool_dispatch(self):
        """call_tool dispatches to the correct handler."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir="/tmp/downloads",
        )
        server = MatriculaMCPServer(config)

        mock_result = _make_mock_page_result(success=True, page=1)
        with patch.object(
            MatriculaDownloader,
            "download_page",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await server.call_tool(
                "inspect_book", {"base_url": "https://example.com/"}
            )

        assert result["accessible"] is True


# ------------------------------------------------------------------
# validate_base_url (standalone function)
# ------------------------------------------------------------------


def test_validate_base_url_valid_https_public_host(monkeypatch):
    """A valid https URL on a public host returns None (no error)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert validate_base_url("https://example.com/") is None


def test_validate_base_url_empty_string():
    """An empty base URL returns an error message."""
    assert validate_base_url("") == "base_url is required"


def test_validate_base_url_http_scheme_rejected():
    """Non-https schemes are rejected."""
    error = validate_base_url("http://example.com/")
    assert error is not None
    assert "Only https scheme" in error


def test_validate_base_url_non_443_port_rejected():
    """Ports other than 443 are rejected."""
    error = validate_base_url("https://example.com:8080/")
    assert error is not None
    assert "Only port 443" in error


def test_validate_base_url_ip_literal_rejected():
    """IP literal hosts are rejected."""
    error = validate_base_url("https://10.0.0.1/")
    assert error is not None
    assert "IP literal" in error


def test_validate_base_url_private_host_rejected(monkeypatch):
    """Hostnames resolving to private addresses are rejected."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://evil.internal.example/")
        assert error is not None
        assert "denied address" in error


def test_validate_base_url_loopback_host_rejected(monkeypatch):
    """Hostnames resolving to loopback are rejected."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://localhost/")
        assert error is not None
        assert "denied address" in error


def test_validate_base_url_credentials_rejected():
    """URLs with userinfo/credentials are rejected."""
    error = validate_base_url("https://user:pass@example.com/")
    assert error is not None
    assert "credentials" in error


def test_validate_base_url_dns_failure_rejected(monkeypatch):
    """DNS resolution failure fails closed with an error message."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise OSError("DNS failure")

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://nonexistent.invalid/")
        assert error is not None
        assert "DNS resolution failed" in error


def test_validate_base_url_ipv6_loopback_rejected(monkeypatch):
    """Hostname resolving to IPv6 loopback is rejected."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://ipv6-loopback.example/")
        assert error is not None
        assert "denied address" in error


def test_validate_base_url_ipv4_mapped_ipv6_private_rejected(monkeypatch):
    """IPv4-mapped IPv6 with private embedded IPv4 is rejected."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("::ffff:10.0.0.1", 443, 0, 0),
            ),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://example.com/")
        assert error is not None
        assert "denied address" in error


def test_validate_base_url_idn_public_host_accepted(monkeypatch):
    """Internationalized domain name on a public host is accepted."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert validate_base_url("https://münchen.example.de/") is None


def test_validate_base_url_no_hostname_rejected():
    """URL without a hostname is rejected."""
    error = validate_base_url("https:///")
    assert error is not None
    assert "hostname" in error.lower()


def test_validate_base_url_none_rejected():
    """None input is rejected."""
    error = validate_base_url(None)
    assert error is not None
    assert "required" in error.lower()


def test_validate_base_url_whitespace_only_rejected():
    """Whitespace-only input is rejected."""
    error = validate_base_url("   ")
    assert error is not None
    assert "empty" in error.lower()


def test_validate_base_url_mixed_dns_rejected(monkeypatch):
    """Mixed public/private DNS responses are rejected (DNS rebinding)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        error = validate_base_url("https://rebinding.example/")
        assert error is not None
        assert "denied address" in error


def test_validate_base_url_with_path_query_fragment_accepted(monkeypatch):
    """URL with path, query, and fragment is accepted if host is valid."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    with patch("matricula_toolkit.security.socket.getaddrinfo", fake_getaddrinfo):
        assert validate_base_url("https://example.com/path/to/page?q=1#section") is None
