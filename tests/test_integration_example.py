"""Integration test with a real example (mocked HTTP).

Demonstrates the full download flow with mocked network responses,
showing that the high-resolution image URL is correctly intercepted
and the image is downloaded.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader


def _make_real_jpeg() -> bytes:
    """Create a minimal valid decodable JPEG image as bytes."""
    img = Image.new("RGB", (1, 1), color="red")
    buffer = BytesIO()
    img.save(buffer, format="JPEG")
    return buffer.getvalue()


def _make_mock_page_with_image(image_url: str, image_data: bytes) -> MagicMock:
    """Create a mock page that captures the image URL and returns image data."""
    page = MagicMock()

    def on_request(event, handler):
        if event == "request":
            # Simulate the browser firing request events
            req_page = MagicMock()
            req_page.url = image_url
            handler(req_page)

    page.on = MagicMock(side_effect=on_request)
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.headers = {"content-type": "image/jpeg"}

    async def fake_get(url, **kwargs):
        assert url == image_url, f"Expected {image_url}, got {url}"
        return mock_response

    page.request.get = AsyncMock(side_effect=fake_get)

    async def fake_body():
        return image_data

    mock_response.body = AsyncMock(side_effect=fake_body)

    return page


@pytest.mark.asyncio
async def test_full_download_flow(tmp_path):
    """Full flow: open page, intercept image URL, download image."""
    config = MatriculaConfig(
        base_url="https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/",
        book="01519",
        out_dir=str(tmp_path),
        wait_seconds=0,
    )
    downloader = MatriculaDownloader(config)

    # The high-resolution image URL pattern from Matricula
    image_url = "https://img.data.matricula-online.eu/image/aGVsbG8vd29ybGQ=?csrf=token123&ctrl=ctrl456"
    image_data = _make_real_jpeg()

    page = _make_mock_page_with_image(image_url, image_data)
    browser = MagicMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    request_context = MagicMock()
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status = 200
    mock_response.headers = {"content-type": "image/jpeg"}

    async def fake_get(url, **kwargs):
        return mock_response

    request_context.get = AsyncMock(side_effect=fake_get)

    async def fake_body():
        return image_data

    mock_response.body = AsyncMock(side_effect=fake_body)

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
    assert result.image_url == image_url

    # Verify the file was written correctly
    assert result.file_path.exists()
    assert result.file_path.read_bytes() == image_data

    # Verify it's a valid JPEG
    valid = await downloader.verify_image(result.file_path)
    assert valid is True


@pytest.mark.asyncio
async def test_download_range_integration(tmp_path):
    """Download a range of pages with mocked browser."""
    config = MatriculaConfig(
        base_url="https://example.com/viewer/",
        book="01519",
        out_dir=str(tmp_path),
        wait_seconds=0,
        max_pages=3,
    )
    downloader = MatriculaDownloader(config)

    image_data = _make_real_jpeg()

    # Create a mock page factory that returns different pages
    page_factory_calls = 0

    def make_page():
        nonlocal page_factory_calls
        page_factory_calls += 1
        page = MagicMock()

        def on_request(event, handler):
            if event == "request":
                req = MagicMock()
                req.url = "https://img.data.matricula-online.eu/image/abc/?csrf=x"
                handler(req)

        page.on = MagicMock(side_effect=on_request)
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.headers = {"content-type": "image/jpeg"}

        async def fake_get(url, **kwargs):
            return mock_response

        page.request.get = AsyncMock(side_effect=fake_get)

        async def fake_body():
            return image_data

        mock_response.body = AsyncMock(side_effect=fake_body)

        return page

    context = MagicMock()
    context.new_page = AsyncMock(side_effect=lambda: make_page())
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)

    request_context = MagicMock()
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status = 200
    mock_response.headers = {"content-type": "image/jpeg"}

    async def fake_get(url, **kwargs):
        return mock_response

    request_context.get = AsyncMock(side_effect=fake_get)

    async def fake_body():
        return image_data

    mock_response.body = AsyncMock(side_effect=fake_body)

    with (
        patch.object(downloader, "_browser", browser),
        patch.object(downloader, "_request_context", request_context),
    ):
        results = await downloader.download_range(104, 200)

    # max_pages=3 limits to 3 downloads
    assert len(results) == 3
    assert all(r.success for r in results)
    assert results[0].page_number == 104
    assert results[1].page_number == 105
    assert results[2].page_number == 106
