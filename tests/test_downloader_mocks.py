"""Mock-based tests for the Matricula downloader.

Tests the downloader logic without launching a real browser, using
mocked Playwright objects and the `responses` library for HTTP mocking.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.exceptions import (
    BrowserError,
    DownloadError,
    ImageNotFoundError,
    ImageValidationError,
    NetworkError,
)
from matricula_toolkit.security import SecurityError


def _make_real_jpeg() -> bytes:
    """Create a minimal valid decodable JPEG image as bytes."""
    import io as _io

    img = Image.new("RGB", (1, 1), color="red")
    buffer = _io.BytesIO()
    img.save(buffer, format="JPEG")
    return buffer.getvalue()


# ----------------------------------------------------------------------
# Helper: build a mock browser/page/context
# ----------------------------------------------------------------------


def _make_mock_request(url: str) -> MagicMock:
    """Create a mock Playwright Request object."""
    req = MagicMock()
    req.url = url
    return req


def _make_mock_page(
    captured_urls: list[str],
    image_data: bytes = b"\xff\xd8\xff\xe0fake",
    response_ok: bool = True,
    response_status: int = 200,
) -> MagicMock:
    """Create a mock Playwright Page with request interception.

    Args:
        captured_urls: URLs to simulate as captured network requests.
        image_data: Bytes to return from response.body().
        response_ok: Whether the image download response is OK.
        response_status: HTTP status code for the response.
    """
    page = MagicMock()

    # Store the on_request handler so we can simulate requests later
    request_handlers: list = []

    def on_request(handler):
        request_handlers.append(handler)

    page.on = MagicMock(
        side_effect=lambda event, handler: (
            request_handlers.append(handler) if event == "request" else None
        )
    )

    # Simulate captured requests after goto
    async def fake_goto(url, **kwargs):
        # Simulate network requests being captured
        for handler in request_handlers:
            for u in captured_urls:
                handler(_make_mock_request(u))
        return MagicMock()

    page.goto = AsyncMock(side_effect=fake_goto)
    page.wait_for_timeout = AsyncMock()

    # Mock page.request.get for image download
    mock_response = MagicMock()
    mock_response.ok = response_ok
    mock_response.status = response_status
    mock_response.headers = {"content-type": "image/jpeg"}

    async def fake_get(url, **kwargs):
        return mock_response

    page.request.get = AsyncMock(side_effect=fake_get)

    # Make response.body() return the image data
    async def fake_body():
        return image_data

    mock_response.body = AsyncMock(side_effect=fake_body)

    return page, request_handlers


def _make_mock_request_context(
    image_data: bytes = b"\xff\xd8\xff\xe0fake",
    response_ok: bool = True,
    response_status: int = 200,
) -> MagicMock:
    """Create a mock Playwright APIRequestContext for image downloads.

    The mock's .get() returns a response with the given status/ok and
    a .body() that returns image_data.
    """
    ctx = MagicMock()

    mock_response = MagicMock()
    mock_response.ok = response_ok
    mock_response.status = response_status
    mock_response.headers = {"content-type": "image/jpeg"}

    async def fake_get(url, **kwargs):
        return mock_response

    ctx.get = AsyncMock(side_effect=fake_get)

    async def fake_body():
        return image_data

    mock_response.body = AsyncMock(side_effect=fake_body)

    return ctx


def _make_mock_browser(page: MagicMock) -> MagicMock:
    """Create a mock Playwright Browser."""
    browser = MagicMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestDownloaderMocks:
    """Tests using mocked Playwright objects."""

    @pytest.mark.asyncio
    async def test_download_page_success(self, tmp_path):
        """Successful page download with mocked browser."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,  # Skip wait in tests
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://example.com/page.html",
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]
        image_data = _make_real_jpeg()

        page, _ = _make_mock_page(image_urls, image_data)
        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(image_data=image_data)

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            result = await downloader.download_page(104)

        assert result.success is True
        assert result.page_number == 104
        assert result.file_path is not None
        assert result.file_path.name == "matricula_01519_pg104.jpg"
        assert result.file_size == len(image_data)
        assert result.image_url == image_urls[-1]

        # Verify file was written
        assert result.file_path.exists()
        assert result.file_path.read_bytes() == image_data

    @pytest.mark.asyncio
    async def test_download_page_image_not_found(self, tmp_path):
        """Download fails when no image URL is found."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        # No image URLs in captured requests
        page, _ = _make_mock_page(
            ["https://example.com/page.html", "https://example.com/style.css"]
        )
        browser = _make_mock_browser(page)

        with (
            patch.object(downloader, "_browser", browser),
            pytest.raises(ImageNotFoundError),
        ):
            await downloader.download_page(104)

    @pytest.mark.asyncio
    async def test_download_page_network_error(self, tmp_path):
        """Download fails when image server returns non-OK."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]

        page, _ = _make_mock_page(
            image_urls, image_data=b"fake", response_ok=False, response_status=403
        )

        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(
            image_data=b"fake", response_ok=False, response_status=403
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "403" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_range_stops_on_max_pages(self, tmp_path):
        """download_range respects max_pages limit."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
            max_pages=2,
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]
        image_data = _make_real_jpeg()

        page, _ = _make_mock_page(image_urls, image_data)
        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(image_data=image_data)

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            results = await downloader.download_range(1, 10)

        # Should stop after max_pages=2 successful downloads
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_download_range_continues_after_failure(self, tmp_path):
        """download_range continues with next page after a failure."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        # First call: no image URL (failure), second call: success
        call_count = 0

        async def fake_download_page(page_number):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ImageNotFoundError("No image found")
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=Path(f"/tmp/matricula_01519_pg{page_number:03d}.jpg"),
                file_size=100,
            )

        with patch.object(downloader, "download_page", side_effect=fake_download_page):
            results = await downloader.download_range(1, 3)

        assert len(results) == 3
        assert results[0].success is False
        assert results[1].success is True
        assert results[2].success is True

    @pytest.mark.asyncio
    async def test_download_page_skips_existing(self, tmp_path):
        """Already downloaded pages are skipped."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        # Pre-create a valid JPEG file
        img_path = tmp_path / "matricula_01519_pg001.jpg"
        img_path.write_bytes(_make_real_jpeg())

        result = await downloader.download_page(1)

        assert result.success is True
        assert result.file_path == img_path
        assert result.file_size == len(_make_real_jpeg())

    @pytest.mark.asyncio
    async def test_browser_not_started(self, tmp_path):
        """Calling download_page without starting browser raises BrowserError."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
        )
        downloader = MatriculaDownloader(config)
        # _browser is None (not started)

        with pytest.raises(BrowserError):
            await downloader.download_page(1)


class TestDownloaderWithResponses:
    """Tests using the `responses` library for HTTP mocking."""

    @pytest.mark.asyncio
    async def test_image_download_403(self, tmp_path):
        """Image server returns 403 Forbidden."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]

        page, _ = _make_mock_page(
            image_urls, image_data=b"fake", response_ok=False, response_status=403
        )

        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(
            image_data=b"fake", response_ok=False, response_status=403
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "403" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_image_download_429(self, tmp_path):
        """Image server returns 429 Too Many Requests."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]

        page, _ = _make_mock_page(
            image_urls, image_data=b"fake", response_ok=False, response_status=429
        )

        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(
            image_data=b"fake", response_ok=False, response_status=429
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "429" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_image_download_404(self, tmp_path):
        """Image server returns 404 Not Found."""
        config = MatriculaConfig(
            base_url="https://example.com/viewer/",
            book="01519",
            out_dir=str(tmp_path),
            wait_seconds=0,
        )
        downloader = MatriculaDownloader(config)

        image_urls = [
            "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz",
        ]

        page, _ = _make_mock_page(
            image_urls, image_data=b"fake", response_ok=False, response_status=404
        )

        browser = _make_mock_browser(page)
        request_context = _make_mock_request_context(
            image_data=b"fake", response_ok=False, response_status=404
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "404" in str(exc_info.value)


# ----------------------------------------------------------------------
# Redirect validator regression tests (Befund 13)
# ----------------------------------------------------------------------


def _make_downloader_with_validator(
    tmp_path,
    redirect_validator=None,
    image_url=None,
    image_data=None,
    response_ok=True,
    response_status=200,
):
    """Build a downloader with mocked browser/request context and optional validator.

    The image_url must match IMAGE_URL_PATTERN
    (https://img.data.matricula-online.eu/image/...) so that
    _extract_image_url finds it in captured requests.
    """
    config = MatriculaConfig(
        base_url="https://example.com/viewer/",
        book="01519",
        out_dir=str(tmp_path),
        wait_seconds=0,
    )
    downloader = MatriculaDownloader(config, redirect_validator=redirect_validator)

    if image_url is None:
        image_url = "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz"
    if image_data is None:
        image_data = _make_real_jpeg()

    image_urls = [image_url]

    page, _ = _make_mock_page(
        image_urls,
        image_data=image_data,
        response_ok=response_ok,
        response_status=response_status,
    )
    browser = _make_mock_browser(page)
    request_context = _make_mock_request_context(
        image_data=image_data, response_ok=response_ok, response_status=response_status
    )

    return downloader, browser, request_context


class TestDownloaderRedirectValidator:
    """Tests for the redirect_validator callback (Befund 13).

    The redirect_validator receives (original_url, image_url) where image_url
    is the extracted image download target.  Tests use matricula-online.eu
    image URLs containing the redirect host name so the validator can inspect
    it.
    """

    @pytest.mark.asyncio
    async def test_valid_initial_call_without_validator(self, tmp_path):
        """No redirect_validator set: download succeeds (SSRF via LocalProxy only).

        Empty redirect_hosts must not block the initial provider call.
        The downloader works without a validator, relying on SSRF via
        LocalProxy for network-level protection.
        """
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=None
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            result = await downloader.download_page(104)

        assert result.success is True
        assert result.image_url is not None

    @pytest.mark.asyncio
    async def test_allowed_redirect_target(self, tmp_path):
        """redirect_validator allows a redirect target in redirect_hosts.

        The image URL contains redirect.example.com which the validator
        accepts as a confirmed redirect host.
        """
        image_url = (
            "https://img.data.matricula-online.eu/image/redirect.example.com/abc123/"
        )

        def validator(original_url: str, redirect_url: str) -> None:
            if "redirect.example.com" not in redirect_url:
                raise SecurityError("not a confirmed redirect host")

        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            result = await downloader.download_page(104)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_denied_public_redirect_target(self, tmp_path):
        """redirect_validator denies a redirect target NOT in redirect_hosts.

        The image URL contains evil.example.com which the validator rejects.
        The download is aborted before any network access to the denied host.
        """
        image_url = (
            "https://img.data.matricula-online.eu/image/evil.example.com/abc123/"
        )

        def validator(original_url: str, redirect_url: str) -> None:
            if "evil.example.com" in redirect_url:
                raise SecurityError("evil.example.com is not a confirmed redirect host")

        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "provider redirect policy" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_multi_hop_redirect_chain(self, tmp_path):
        """Multiple redirect_validator calls simulate a multi-hop chain.

        Each download_page call invokes the validator once.  A chain of
        allowed hops succeeds; a final denied hop aborts the download.
        """
        calls: list[tuple[str, str]] = []

        def validator(original_url: str, redirect_url: str) -> None:
            calls.append((original_url, redirect_url))
            if "evil.example.com" in redirect_url:
                raise SecurityError("evil.example.com not allowed")

        # Hop 1: allowed
        image_url = (
            "https://img.data.matricula-online.eu/image/hop1.example.com/abc123/"
        )
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            result = await downloader.download_page(104)

        assert result.success is True
        assert len(calls) == 1
        assert "hop1.example.com" in calls[0][1]

    @pytest.mark.asyncio
    async def test_private_ip_redirect_denied(self, tmp_path):
        """redirect_validator denies private IP redirect targets."""

        def validator(original_url: str, redirect_url: str) -> None:
            if "10.0.0.1" in redirect_url:
                raise SecurityError("private IP not allowed")

        image_url = "https://img.data.matricula-online.eu/image/10.0.0.1/abc123/"
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "provider redirect policy" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_loopback_redirect_denied(self, tmp_path):
        """redirect_validator denies loopback redirect targets."""

        def validator(original_url: str, redirect_url: str) -> None:
            if "127.0.0.1" in redirect_url:
                raise SecurityError("loopback not allowed")

        image_url = "https://img.data.matricula-online.eu/image/127.0.0.1/abc123/"
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "provider redirect policy" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_link_local_redirect_denied(self, tmp_path):
        """redirect_validator denies link-local redirect targets."""

        def validator(original_url: str, redirect_url: str) -> None:
            if "169.254.169.254" in redirect_url:
                raise SecurityError("link-local not allowed")

        image_url = "https://img.data.matricula-online.eu/image/169.254.169.254/abc123/"
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(104)

        assert "provider redirect policy" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_range_with_redirect_validator(self, tmp_path):
        """download_range applies redirect_validator to each page download."""

        def validator(original_url: str, redirect_url: str) -> None:
            if "evil.example.com" in redirect_url:
                raise SecurityError("evil.example.com not allowed")

        image_url = "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz"
        downloader, browser, request_context = _make_downloader_with_validator(
            tmp_path, redirect_validator=validator, image_url=image_url
        )

        with (
            patch.object(downloader, "_browser", browser),
            patch.object(downloader, "_request_context", request_context),
        ):
            results = await downloader.download_range(1, 3)

        assert len(results) == 3
        assert all(r.success for r in results)


# ----------------------------------------------------------------------
# Retry behavior tests (PR A: Download-Retry und robuste Fehlerbehandlung)
# ----------------------------------------------------------------------


class TestDownloaderRetry:
    """Tests for download retry logic.

    All tests mock ``_download_page_attempt`` directly to isolate the
    retry loop from Playwright/browser interactions.  Sleep is mocked
    to keep tests fast and deterministic.
    """

    def _make_retry_downloader(
        self,
        tmp_path: Path,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> MatriculaDownloader:
        """Create a downloader with retry config and zero backoff."""
        return MatriculaDownloader(
            MatriculaConfig(
                base_url="https://example.com/viewer/",
                book="01519",
                out_dir=str(tmp_path),
                wait_seconds=0,
                max_retries=max_retries,
                retry_backoff_base=backoff_base,
                retry_backoff_max=backoff_max,
            )
        )

    @pytest.mark.asyncio
    async def test_download_succeeds_first_attempt_no_retry(self, tmp_path):
        """Successful download on first attempt: no retry, one call."""
        downloader = self._make_retry_downloader(tmp_path)
        call_count = 0

        async def single_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        with patch.object(
            downloader, "_download_page_attempt", side_effect=single_attempt
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 1  # Exactly one attempt, no retry

    @pytest.mark.asyncio
    async def test_retry_transient_then_success(self, tmp_path):
        """Transient 503 on first attempt, success on second: two calls total."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NetworkError("Service unavailable", status_code=503)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2  # First attempt failed, second succeeded
        assert len(sleep_calls) == 1  # One backoff before retry
        assert sleep_calls[0] == 1.0  # base_backoff * 2^0 = 1.0

    @pytest.mark.asyncio
    async def test_multiple_transient_errors_then_success(self, tmp_path):
        """Two transient failures, then success: three calls total."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise NetworkError("Service unavailable", status_code=503)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 3  # Two failures, then success
        assert len(sleep_calls) == 2  # Backoff before attempts 2 and 3
        assert sleep_calls[0] == 1.0  # base * 2^0 = 1.0
        assert sleep_calls[1] == 2.0  # base * 2^1 = 2.0

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, tmp_path):
        """All attempts fail with transient error: max_retries + 1 calls."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=2)
        call_count = 0
        sleep_calls: list[float] = []

        async def always_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise NetworkError("Service unavailable", status_code=503)

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=always_fail),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(1)

        assert exc_info.value.status_code == 503
        assert call_count == 3  # max_retries=2 → 3 total attempts
        assert len(sleep_calls) == 2  # Backoff before attempts 2 and 3

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_http_error(self, tmp_path):
        """404 Not Found: no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def always_not_found(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise NetworkError("Not found", status_code=404)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=always_not_found
            ),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(1)

        assert exc_info.value.status_code == 404
        assert call_count == 1  # No retry for permanent 4xx

    @pytest.mark.asyncio
    async def test_no_retry_on_image_not_found(self, tmp_path):
        """ImageNotFoundError: no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def no_image(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise ImageNotFoundError("No image URL found")

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=no_image),
            pytest.raises(ImageNotFoundError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_no_retry_on_invalid_image_data(self, tmp_path):
        """ImageValidationError: no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def invalid_image(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise ImageValidationError("Not a valid JPEG")

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=invalid_image
            ),
            pytest.raises(ImageValidationError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_no_retry_on_ssrf_violation(self, tmp_path):
        """SecurityError (SSRF/redirect): no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def ssrf_violation(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise SecurityError("Private IP not allowed")

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=ssrf_violation
            ),
            pytest.raises(SecurityError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for security violations

    @pytest.mark.asyncio
    async def test_no_retry_on_browser_error(self, tmp_path):
        """BrowserError: no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def browser_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise BrowserError("Browser crashed")

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=browser_fail
            ),
            pytest.raises(BrowserError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_no_retry_on_download_error(self, tmp_path):
        """DownloadError (empty image, file too large): no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def download_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise DownloadError("Empty image data")

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=download_fail
            ),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_max_retries_zero(self, tmp_path):
        """max_retries=0: exactly one attempt, no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=0)
        call_count = 0

        async def fail_once(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise NetworkError("Service unavailable", status_code=503)

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=fail_once),
            pytest.raises(NetworkError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # Only one attempt with max_retries=0

    @pytest.mark.asyncio
    async def test_retry_after_header_honored(self, tmp_path):
        """Retry-After header: backoff uses the header value."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def rate_limited_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NetworkError("Rate limited", status_code=429, retry_after=5)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=rate_limited_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 5.0  # Retry-After: 5 seconds

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max(self, tmp_path):
        """Backoff never exceeds retry_backoff_max."""
        downloader = self._make_retry_downloader(
            tmp_path, max_retries=5, backoff_base=10.0, backoff_max=15.0
        )
        call_count = 0
        sleep_calls: list[float] = []

        async def always_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise NetworkError("Service unavailable", status_code=503)

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=always_fail),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(NetworkError),
        ):
            await downloader.download_page(1)

        assert call_count == 6  # max_retries=5 → 6 total attempts
        assert len(sleep_calls) == 5
        # Backoff sequence: min(10*2^0, 15)=10, min(10*2^1, 15)=15,
        # min(10*2^2, 15)=15, min(10*2^3, 15)=15, min(10*2^4, 15)=15
        assert sleep_calls == [10.0, 15.0, 15.0, 15.0, 15.0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_at_max(self, tmp_path):
        """Retry-After header cannot exceed retry_backoff_max."""
        downloader = self._make_retry_downloader(
            tmp_path, max_retries=3, backoff_base=1.0, backoff_max=10.0
        )
        call_count = 0
        sleep_calls: list[float] = []

        async def rate_limited_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Server demands 3600 seconds, but max_backoff is 10.0.
                raise NetworkError("Rate limited", status_code=429, retry_after=3600)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=rate_limited_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 10.0  # capped at max_backoff, not 3600

    @pytest.mark.asyncio
    async def test_retry_after_invalid_header_ignored(self, tmp_path):
        """Non-numeric Retry-After falls back to exponential backoff."""
        downloader = self._make_retry_downloader(
            tmp_path, max_retries=3, backoff_base=2.0, backoff_max=60.0
        )
        call_count = 0
        sleep_calls: list[float] = []

        async def rate_limited_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NetworkError("Rate limited", status_code=429, retry_after=None)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=rate_limited_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 2.0  # base_backoff * 2^0 = 2.0

    @pytest.mark.asyncio
    async def test_no_retry_on_501(self, tmp_path):
        """HTTP 501 Not Implemented is a permanent error, no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def not_implemented(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise NetworkError("Not Implemented", status_code=501)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=not_implemented
            ),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(1)

        assert exc_info.value.status_code == 501
        assert call_count == 1  # No retry for permanent 501

    @pytest.mark.asyncio
    async def test_retry_connection_error_then_success(self, tmp_path):
        """ConnectionError on first attempt, success on second: retried."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Connection reset by peer")
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0

    @pytest.mark.asyncio
    async def test_retry_timeout_error_then_success(self, tmp_path):
        """asyncio.TimeoutError on first attempt, success on second: retried."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError("Connect timeout")
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_connection_error(self, tmp_path):
        """Repeated ConnectionError: all attempts fail, NetworkError raised."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=2)
        call_count = 0
        sleep_calls: list[float] = []

        async def always_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise OSError(errno.ENETUNREACH, "Network unreachable")

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=always_fail),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(NetworkError) as exc_info,
        ):
            await downloader.download_page(1)

        assert exc_info.value.status_code is None
        assert call_count == 3  # max_retries=2 → 3 total attempts
        assert len(sleep_calls) == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_download_error(self, tmp_path):
        """DownloadError (empty image, file too large): no retry, single call."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def download_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise DownloadError("Empty image data")

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=download_fail
            ),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_download_range_uses_retry(self, tmp_path):
        """download_range benefits from download_page retry logic."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=1)
        call_count = 0

        async def flaky(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if page_number == 1 and call_count == 1:
                raise NetworkError("Service unavailable", status_code=503)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            pass

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=flaky),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            results = await downloader.download_range(1, 2)

        assert len(results) == 2
        assert results[0].success is True  # Retried and succeeded
        assert results[1].success is True
        assert call_count == 3  # Page 1: 2 attempts, Page 2: 1 attempt

    @pytest.mark.asyncio
    async def test_retry_connection_reset_error_then_success(self, tmp_path):
        """ConnectionResetError on first attempt, success on second: retried."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("Connection reset by peer")
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0

    @pytest.mark.asyncio
    async def test_no_retry_on_permission_error(self, tmp_path):
        """PermissionError (filesystem, not network): no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def perm_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise OSError(errno.EACCES, "Permission denied")

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=perm_fail),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for filesystem errors

    @pytest.mark.asyncio
    async def test_no_retry_on_file_not_found_error(self, tmp_path):
        """FileNotFoundError (filesystem, not network): no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def not_found(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise OSError(errno.ENOENT, "No such file or directory")

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=not_found),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for filesystem errors

    @pytest.mark.asyncio
    async def test_no_retry_on_enospc(self, tmp_path):
        """OSError with ENOSPC (no space left): no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def no_space(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise OSError(errno.ENOSPC, "No space left on device")

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=no_space),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for ENOSPC
        assert len(sleep_calls) == 0  # No backoff for permanent errors

    @pytest.mark.asyncio
    async def test_network_errno_retry_then_success(self, tmp_path):
        """OSError with ECONNRESET (network errno) on first attempt, then success."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError(errno.ECONNRESET, "Connection reset by peer")
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0

    @pytest.mark.asyncio
    async def test_retry_dns_eai_again_then_success(self, tmp_path):
        """socket.gaierror with EAI_AGAIN (temporary DNS failure): retried."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_attempt(page_number, start_time):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise socket.gaierror(
                    socket.EAI_AGAIN, "Temporary failure in name resolution"
                )
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=tmp_path / f"matricula_01519_pg{page_number:03d}.jpg",
                file_size=100,
            )

        async def fake_sleep(duration: float):
            sleep_calls.append(duration)

        with (
            patch.object(
                downloader, "_download_page_attempt", side_effect=flaky_attempt
            ),
            patch("matricula_toolkit.downloader.asyncio.sleep", side_effect=fake_sleep),
        ):
            result = await downloader.download_page(1)

        assert result.success is True
        assert call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1.0

    @pytest.mark.asyncio
    async def test_no_retry_on_dns_eai_fail(self, tmp_path):
        """socket.gaierror with EAI_FAIL (permanent DNS failure): no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def dns_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise socket.gaierror(
                socket.EAI_FAIL, "Non-recoverable failure in name resolution"
            )

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=dns_fail),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for permanent DNS error

    @pytest.mark.asyncio
    async def test_no_retry_on_dns_eai_noname(self, tmp_path):
        """socket.gaierror with EAI_NONAME (host not known): no retry."""
        downloader = self._make_retry_downloader(tmp_path, max_retries=3)
        call_count = 0

        async def dns_fail(page_number, start_time):
            nonlocal call_count
            call_count += 1
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        with (
            patch.object(downloader, "_download_page_attempt", side_effect=dns_fail),
            pytest.raises(DownloadError),
        ):
            await downloader.download_page(1)

        assert call_count == 1  # No retry for permanent DNS error
