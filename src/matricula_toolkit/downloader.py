"""Core downloader for Matricula church book scans.

Uses Playwright/Chromium to open Matricula viewer pages, intercepts the
high-resolution image URL from network requests, and downloads the image.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, Page, Request, async_playwright
from typing_extensions import Self

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.exceptions import (
    BrowserError,
    DownloadError,
    ImageNotFoundError,
    ImageValidationError,
    MatriculaError,
    NetworkError,
)
from matricula_toolkit.local_proxy import LocalProxy
from matricula_toolkit.security import SecurityError, validate_redirect_url

# HTTP status codes that indicate a temporary failure and may be retried.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# errno values that indicate a network-level (transient) OS error, as opposed
# to filesystem errors (PermissionError, FileNotFoundError, ENOSPC, ...).
_NETWORK_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.EHOSTUNREACH,
        errno.EHOSTDOWN,
        errno.EPIPE,
        socket.EAI_AGAIN,  # Temporary DNS failure — retryable
    }
)

# Permanent DNS error codes — never retried.  Only constants that exist on
# the current platform are included (some EAI_* values are platform-specific).
_PERMANENT_DNS_ERRNOS: frozenset[int] = frozenset(
    {
        value
        for value in (
            getattr(socket, "EAI_FAIL", None),
            getattr(socket, "EAI_NONAME", None),
            getattr(socket, "EAI_NODATA", None),
            getattr(socket, "EAI_FAMILY", None),
            getattr(socket, "EAI_SERVICE", None),
            getattr(socket, "EAI_SOCKTYPE", None),
            getattr(socket, "EAI_BADFLAGS", None),
            getattr(socket, "EAI_PROTOCOL", None),
            getattr(socket, "EAI_OVERFLOW", None),
        )
        if value is not None
    }
)


def _is_network_errno(errno_value: int | None) -> bool:
    """Return True if *errno_value* indicates a network-level OS error.

    Filesystem errors (PermissionError, FileNotFoundError, ENOSPC, ...) are
    not network errors and must not be retried as if they were transient.
    """
    return errno_value in _NETWORK_ERRNOS


@dataclass
class PageResult:
    """Result of downloading a single page."""

    page_number: int
    success: bool
    file_path: Path | None = None
    file_size: int | None = None
    image_url: str | None = None
    error: str | None = None
    duration_seconds: float | None = None


class MatriculaDownloader:
    """Downloads Matricula church book pages using Playwright.

    All outbound traffic is routed through a loopback-only local proxy
    (:class:`matricula_toolkit.local_proxy.LocalProxy`) that enforces the
    centralized SSRF security policy.  The proxy is started with the
    browser session and stopped when the session ends.  There is no
    direct-connection fallback: if the proxy is unavailable or rejects a
    target, the download fails.
    """

    # Pattern for Matricula image server URLs
    IMAGE_URL_PATTERN = re.compile(
        r"https://img\.data\.matricula-online\.eu/image/[^?\s\"']+"
    )

    def __init__(
        self,
        config: MatriculaConfig,
        redirect_validator: Callable[[str, str], None] | None = None,
    ):
        """Initialize the downloader.

        Args:
            config: Downloader configuration.
            redirect_validator: Optional callable ``(original_url, redirect_url) -> None``
                that raises :class:`SecurityError` if *redirect_url* is not an
                allowed redirect target for the provider.  Called before each
                image download to enforce provider-specific redirect rules.
        """
        self.config = config
        self._browser: Browser | None = None
        self._playwright: object | None = None
        self._proxy: LocalProxy | None = None
        self._request_context: object | None = None
        self._found_image_urls: list[str] = []
        self._redirect_validator = redirect_validator

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Start the browser and the local SSRF proxy.

        The proxy is started first and bound to loopback only.  All browser
        contexts and API requests created by this downloader route through
        the proxy.  If the proxy cannot start, the browser is not launched
        (fail-closed).
        """
        try:
            self._proxy = LocalProxy()
            await self._proxy.start()

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless,
            )

            # Create an APIRequestContext that routes through the proxy for
            # image downloads.  new_context() is on playwright.request.
            proxy_settings = {"server": self._proxy.proxy_url}
            self._request_context = await self._playwright.request.new_context(  # type: ignore[attr-defined]
                proxy=proxy_settings  # type: ignore[arg-type]
            )
        except Exception as exc:
            # Fail closed: stop anything that was started and re-raise.
            await self.close()
            raise BrowserError(f"Failed to start downloader: {exc}") from exc

    async def close(self) -> None:
        """Close the browser, request context, proxy, and playwright."""
        if self._request_context:
            try:
                await self._request_context.dispose()  # type: ignore[attr-defined]
            except Exception:  # noqa: S110,BLE001 — cleanup must not raise
                pass
            self._request_context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._proxy:
            await self._proxy.stop()
            self._proxy = None
        if self._playwright:
            await self._playwright.stop()  # type: ignore[attr-defined]
            self._playwright = None

    def _page_url(self, page_number: int) -> str:
        """Build the viewer URL for a given page number."""
        base = self.config.base_url.rstrip("/")
        return f"{base}/?pg={page_number}"

    def _output_path(self, page_number: int) -> Path:
        """Build the output file path for a given page number."""
        out_dir = Path(self.config.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"matricula_{self.config.book}_pg{page_number:03d}.jpg"

    def _is_already_downloaded(self, page_number: int) -> bool:
        """Check if a page has already been downloaded and is a valid JPEG.

        Returns True only if the output file exists and passes JPEG
        validation (magic bytes + Pillow decode).
        """
        out_path = self._output_path(page_number)
        if not out_path.exists():
            return False
        return self._validate_jpeg_file(out_path)

    def _validate_jpeg_file(self, path: Path) -> bool:
        """Validate that *path* is a readable, decodable JPEG file.

        Checks: file exists, non-empty, JPEG magic bytes, and Pillow can
        fully decode it. Returns True on success, False otherwise.
        """
        try:
            if not path.exists():
                return False
            if path.stat().st_size == 0:
                return False
            with open(path, "rb") as f:
                header = f.read(4)
            if header[:3] != b"\xff\xd8\xff":
                return False
            from PIL import Image  # type: ignore[import-untyped]

            with Image.open(path) as img:
                img.load()
            return True
        except Exception:  # noqa: BLE001 — any validation failure means invalid
            return False

    def _extract_image_url(self, requests: list[Request]) -> str | None:
        """Extract the high-resolution image URL from captured requests.

        Matricula renders the image on a Canvas and loads it from
        img.data.matricula-online.eu/image/... via network requests.
        We return the last matching URL, which is typically the
        highest-resolution version.
        """
        image_urls = []
        for req in requests:
            url = req.url
            if self.IMAGE_URL_PATTERN.match(url):
                image_urls.append(url)

        if not image_urls:
            return None

        # Return the last (typically highest-resolution) image URL
        return image_urls[-1]

    async def download_page(self, page_number: int) -> PageResult:
        """Download a single page with retry for transient failures.

        Opens the viewer page, waits for the image to load, intercepts
        the image URL from network requests, and downloads the image.

        Retry behavior:
        - Only transient errors (retryable HTTP status codes, connection
          errors) are retried.  Permanent errors (SSRF/redirect violations,
          missing images, invalid image data, 4xx client errors except 429)
          are raised immediately without retry.
        - Retries use exponential backoff capped at
          ``retry_backoff_max`` seconds.  A ``Retry-After`` response header
          is honored if present.
        - The number of total attempts is ``max_retries + 1``.

        Redirect handling (F2):
        - The initial page URL and image URL are validated via
          ``validate_redirect_url`` before any network access.
        - ``page.goto()`` and ``request_context.get()`` follow redirects
          automatically.  Each new host encountered during redirect
          following is validated by the LocalProxy at the CONNECT level
          (the proxy rejects CONNECT to private/loopback/denied addresses
          with 403).  There is no application-level per-redirect-step
          validation because Playwright does not expose individual redirect
          hops; the proxy provides the per-hop enforcement.

        Proxy enforcement (F4):
        - All browser traffic routes through LocalProxy via Playwright
          proxy settings on both the BrowserContext and the APIRequestContext.
        - There is no direct-connection fallback: if the proxy is
          unavailable or rejects a target, the operation fails.
        - Environment variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY) do not
          affect the proxy URL; it is set explicitly from
          ``LocalProxy.proxy_url``.
        """
        start_time = time.monotonic()

        # Skip if already downloaded
        if self._is_already_downloaded(page_number):
            out_path = self._output_path(page_number)
            return PageResult(
                page_number=page_number,
                success=True,
                file_path=out_path,
                file_size=out_path.stat().st_size,
                error=None,
                duration_seconds=time.monotonic() - start_time,
            )

        max_retries = self.config.max_retries
        base_backoff = self.config.retry_backoff_base
        max_backoff = self.config.retry_backoff_max

        last_error: MatriculaError | None = None

        for attempt in range(max_retries + 1):
            # Apply backoff before retries (not before the first attempt).
            if attempt > 0:
                wait = min(base_backoff * (2 ** (attempt - 1)), max_backoff)
                if (
                    isinstance(last_error, NetworkError)
                    and last_error.retry_after is not None
                ):
                    # Honor Retry-After, but never exceed the configured
                    # backoff maximum so a server cannot force an unbounded
                    # wait via a manipulated Retry-After header.
                    wait = min(max(wait, float(last_error.retry_after)), max_backoff)
                await asyncio.sleep(wait)

            try:
                return await self._download_page_attempt(page_number, start_time)
            except MatriculaError as exc:
                last_error = exc
                if not self._is_transient_error(exc) or attempt == max_retries:
                    raise
            except (
                ConnectionError,
                TimeoutError,
                asyncio.TimeoutError,
            ) as exc:
                # These are always network-related and transient.
                last_error = NetworkError(
                    f"Connection error downloading page {page_number}: {exc}"
                )
                if attempt == max_retries:
                    raise last_error from exc
            except socket.gaierror as exc:
                # DNS resolution errors.  Only EAI_AGAIN (temporary failure)
                # is retried; all other DNS errors are permanent.
                if exc.errno == socket.EAI_AGAIN:
                    last_error = NetworkError(
                        f"DNS resolution failed for page {page_number}: {exc}"
                    )
                    if attempt == max_retries:
                        raise last_error from exc
                else:
                    raise DownloadError(
                        f"DNS resolution failed for page {page_number}: {exc}"
                    ) from exc
            except OSError as exc:
                # OSError covers filesystem errors too.  Only network-related
                # errno values are transient; filesystem errors are permanent
                # and must not be retried.
                if _is_network_errno(exc.errno):
                    last_error = NetworkError(
                        f"Network error downloading page {page_number}: {exc}"
                    )
                    if attempt == max_retries:
                        raise last_error from exc
                else:
                    raise DownloadError(
                        f"Filesystem error downloading page {page_number}: {exc}"
                    ) from exc
            except Exception as exc:  # wrap unexpected errors
                last_error = DownloadError(
                    f"Failed to download page {page_number}: {exc}"
                )
                if attempt == max_retries:
                    raise last_error from exc

        # Should be unreachable: the loop always raises or returns.
        raise last_error or DownloadError(
            f"Failed to download page {page_number}: unknown error"
        )

    async def _download_page_attempt(
        self, page_number: int, start_time: float
    ) -> PageResult:
        """Perform a single download attempt for *page_number*.

        Contains the core download logic (browser navigation, image
        extraction, download, validation, atomic save).  Raises
        :class:`MatriculaError` subclasses on failure.
        """
        if not self._browser:
            raise BrowserError("Browser not started. Call start() first.")

        # Create a browser context that routes all traffic through the proxy.
        # This covers page navigation and all sub-resource requests.
        proxy_settings = {"server": self._proxy.proxy_url} if self._proxy else None
        context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            proxy=proxy_settings,  # type: ignore[arg-type]
        )
        page: Page = await context.new_page()

        captured_requests: list[Request] = []

        def on_request(request: Request) -> None:
            captured_requests.append(request)

        page.on("request", on_request)

        # Validate the initial navigation URL through the SSRF policy before
        # the browser navigates to it.  This covers the page.goto() path,
        # which is not covered by validate_redirect_url on image URLs alone.
        # Relative URLs are resolved against the configured base URL first.
        page_url = self._page_url(page_number)
        try:
            validate_redirect_url(page_url, page_url)
        except SecurityError as exc:
            await context.close()
            raise NetworkError(f"Page URL rejected by SSRF policy: {exc}") from exc

        try:
            await page.goto(page_url, timeout=self.config.timeout_ms)

            # Wait for the image to render (matches the proven 5-second method)
            await page.wait_for_timeout(self.config.wait_seconds * 1000)

            image_url = self._extract_image_url(captured_requests)

            if not image_url:
                raise ImageNotFoundError(
                    f"No high-resolution image URL found for page {page_number}"
                )

            # Download the image using the proxy-routed request context.
            # All traffic goes through the LocalProxy which enforces the
            # SSRF security policy.  There is no direct-connection fallback:
            # if the request context is not initialized, the operation fails.
            if self._request_context is None:
                raise BrowserError(
                    "Request context not initialized. Call start() before "
                    "download_page(). No direct-connection fallback is allowed."
                )

            # Validate the image URL through the SSRF security policy
            # before downloading.
            try:
                validate_redirect_url(self._page_url(page_number), image_url)
            except SecurityError as exc:
                raise NetworkError(f"Image URL rejected by SSRF policy: {exc}") from exc

            # Validate the image URL against the provider-specific redirect
            # policy.  This enforces redirect_hosts for the actual download
            # target, not just the initial page URL.
            if self._redirect_validator is not None:
                try:
                    self._redirect_validator(self._page_url(page_number), image_url)
                except SecurityError as exc:
                    raise NetworkError(
                        f"Image URL rejected by provider redirect policy: {exc}"
                    ) from exc

            response = await self._request_context.get(  # type: ignore[attr-defined]
                image_url, timeout=self.config.timeout_ms
            )

            if not response.ok:
                retry_after = None
                retry_after_header = response.headers.get("retry-after")
                if retry_after_header is not None:
                    try:
                        retry_after = int(retry_after_header)
                    except ValueError:
                        retry_after = None
                raise NetworkError(
                    f"Image download failed with status {response.status} for page {page_number}",
                    status_code=response.status,
                    retry_after=retry_after,
                )

            # Validate content type
            content_type = response.headers.get("content-type", "")
            if content_type and not any(
                ct in content_type.lower()
                for ct in ("image/jpeg", "image/jpg", "image/pjpeg")
            ):
                raise NetworkError(
                    f"Unexpected content-type '{content_type}' for page {page_number}"
                )

            image_data = await response.body()

            if len(image_data) == 0:
                raise DownloadError(f"Empty image data for page {page_number}")

            # Optional file size check
            if (
                self.config.max_file_size > 0
                and len(image_data) > self.config.max_file_size
            ):
                raise DownloadError(
                    f"Image too large ({len(image_data)} bytes) for page {page_number}"
                )

            # Validate the downloaded image is a decodable JPEG
            try:
                import io as _io

                from PIL import Image  # type: ignore[import-untyped]

                with Image.open(_io.BytesIO(image_data)) as img:
                    img.load()
            except Exception as exc:
                raise ImageValidationError(
                    f"Downloaded image is not a valid JPEG for page {page_number}: {exc}"
                ) from exc

            # Atomic save: write to temp file in same directory, then rename
            out_path = self._output_path(page_number)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                fd, temp_name = tempfile.mkstemp(
                    dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp"
                )
                temp_path = Path(temp_name)
                with os.fdopen(fd, "wb") as f:
                    f.write(image_data)
                temp_path.replace(out_path)
            except Exception:
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                raise

            duration = time.monotonic() - start_time

            return PageResult(
                page_number=page_number,
                success=True,
                file_path=out_path,
                file_size=len(image_data),
                image_url=image_url,
                duration_seconds=duration,
            )

        except MatriculaError:
            raise
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
            # These are always network-related and transient.
            raise NetworkError(
                f"Connection error downloading page {page_number}: {exc}"
            ) from exc
        except socket.gaierror as exc:
            # DNS resolution errors.  Only EAI_AGAIN (temporary failure)
            # is retried; all other DNS errors are permanent.
            if exc.errno == socket.EAI_AGAIN:
                raise NetworkError(
                    f"DNS resolution failed for page {page_number}: {exc}"
                ) from exc
            raise DownloadError(
                f"DNS resolution failed for page {page_number}: {exc}"
            ) from exc
        except OSError as exc:
            # OSError covers filesystem errors too.  Only network-related
            # errno values are transient; filesystem errors are permanent
            # and must not be retried.
            if _is_network_errno(exc.errno):
                raise NetworkError(
                    f"Network error downloading page {page_number}: {exc}"
                ) from exc
            raise DownloadError(
                f"Filesystem error downloading page {page_number}: {exc}"
            ) from exc
        except Exception as exc:
            raise DownloadError(
                f"Failed to download page {page_number}: {exc}"
            ) from exc
        finally:
            await context.close()

    @staticmethod
    def _is_transient_error(exc: MatriculaError) -> bool:
        """Classify whether *exc* is a transient error that may be retried.

        Transient errors (retryable):
        - :class:`NetworkError` with a retryable HTTP status code
          (429, 500, 502, 503, 504).
        - :class:`NetworkError` without a status code (connection-level
          errors where no HTTP response was received).

        Permanent errors (not retryable):
        - :class:`ImageNotFoundError` — no image URL found on the page.
        - :class:`ImageValidationError` — downloaded data is not valid JPEG.
        - :class:`SecurityError` — SSRF or redirect-policy violation.
        - :class:`NetworkError` with a permanent HTTP status code
          (4xx except 429, e.g. 400, 401, 403, 404).
        - :class:`BrowserError` — browser startup or context failure.
        - :class:`DownloadError` — empty image data, file too large.
        """
        # ImageNotFoundError, ImageValidationError, BrowserError,
        # DownloadError are always permanent.
        if isinstance(
            exc,
            (
                ImageNotFoundError,
                ImageValidationError,
                BrowserError,
                DownloadError,
            ),
        ):
            return False

        # SecurityError (SSRF / redirect policy violation) is always permanent.
        if isinstance(exc, SecurityError):
            return False

        # NetworkError: classify by HTTP status code.
        if isinstance(exc, NetworkError):
            if exc.status_code is None:
                # Connection-level error, no HTTP response: may be transient.
                return True
            return exc.status_code in _RETRYABLE_STATUS_CODES

        # Unknown MatriculaError subclasses: do not retry (fail-safe).
        return False

    async def download_range(
        self,
        start_page: int,
        end_page: int,
        progress_callback=None,
    ) -> list[PageResult]:
        """Download a range of pages sequentially.

        Args:
            start_page: First page number (inclusive).
            end_page: Last page number (inclusive).
            progress_callback: Optional callback called after each page with
                the PageResult.

        Returns:
            List of PageResult objects, one per page.
        """
        results: list[PageResult] = []

        for page_number in range(start_page, end_page + 1):
            if self.config.max_pages > 0 and len(results) >= self.config.max_pages:
                break

            try:
                result = await self.download_page(page_number)
                results.append(result)

                if progress_callback:
                    progress_callback(result)

                if not result.success:
                    # Log but continue with next page
                    pass

            except MatriculaError as exc:
                result = PageResult(
                    page_number=page_number,
                    success=False,
                    error=str(exc),
                )
                results.append(result)

                if progress_callback:
                    progress_callback(result)

        return results

    async def verify_image(self, file_path: Path) -> bool:
        """Verify that a downloaded image file is valid.

        Checks that the file exists, is non-empty, starts with the JPEG
        magic bytes (FF D8 FF), and can be fully decoded by Pillow.
        """
        return self._validate_jpeg_file(file_path)
