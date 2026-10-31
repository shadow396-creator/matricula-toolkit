"""Tests for the Matricula downloader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.exceptions import (
    ConfigurationError,
)


class TestMatriculaConfig:
    """Tests for MatriculaConfig."""

    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.setenv("MATRICULA_BASE_URL", "https://example.com/viewer/")
        monkeypatch.setenv("MATRICULA_BOOK", "01519")
        config = MatriculaConfig.from_env()
        assert config.base_url == "https://example.com/viewer/"
        assert config.book == "01519"
        assert config.wait_seconds == 5
        assert config.headless is True

    def test_validate_missing_url(self):
        config = MatriculaConfig(base_url="", book="01519")
        with pytest.raises(ConfigurationError):
            config.validate()

    def test_validate_missing_book(self):
        config = MatriculaConfig(base_url="https://example.com/", book="")
        with pytest.raises(ConfigurationError):
            config.validate()

    def test_validate_wait_too_low(self):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", wait_seconds=0
        )
        with pytest.raises(ConfigurationError):
            config.validate()


class TestMatriculaDownloader:
    """Tests for MatriculaDownloader."""

    def test_output_path(self):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir="/tmp/downloads"
        )
        downloader = MatriculaDownloader(config)
        path = downloader._output_path(104)
        assert path == Path("/tmp/downloads/matricula_01519_pg104.jpg")

    def test_output_path_padding(self):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir="/tmp/downloads"
        )
        downloader = MatriculaDownloader(config)
        path = downloader._output_path(1)
        assert path.name == "matricula_01519_pg001.jpg"
        path = downloader._output_path(303)
        assert path.name == "matricula_01519_pg303.jpg"

    def test_page_url(self):
        config = MatriculaConfig(base_url="https://example.com/viewer/", book="01519")
        downloader = MatriculaDownloader(config)
        url = downloader._page_url(104)
        assert url == "https://example.com/viewer/?pg=104"

    def test_extract_image_url(self):
        config = MatriculaConfig(base_url="https://example.com/", book="01519")
        downloader = MatriculaDownloader(config)

        # Mock requests
        req1 = MagicMock()
        req1.url = "https://example.com/page.html"
        req2 = MagicMock()
        req2.url = "https://img.data.matricula-online.eu/image/abc123/?csrf=xyz"
        req3 = MagicMock()
        req3.url = "https://img.data.matricula-online.eu/image/def456/?csrf=uvw"

        url = downloader._extract_image_url([req1, req2, req3])
        assert url == "https://img.data.matricula-online.eu/image/def456/?csrf=uvw"

    def test_extract_image_url_none(self):
        config = MatriculaConfig(base_url="https://example.com/", book="01519")
        downloader = MatriculaDownloader(config)

        req1 = MagicMock()
        req1.url = "https://example.com/page.html"
        req2 = MagicMock()
        req2.url = "https://example.com/style.css"

        url = downloader._extract_image_url([req1, req2])
        assert url is None

    def test_extract_image_url_empty(self):
        config = MatriculaConfig(base_url="https://example.com/", book="01519")
        downloader = MatriculaDownloader(config)
        url = downloader._extract_image_url([])
        assert url is None

    def test_is_already_downloaded(self, tmp_path):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir=str(tmp_path)
        )
        downloader = MatriculaDownloader(config)

        # File doesn't exist yet
        assert downloader._is_already_downloaded(1) is False

        # Create a valid decodable JPEG file
        import io as _io

        from PIL import Image

        img = Image.new("RGB", (1, 1), color="red")
        buffer = _io.BytesIO()
        img.save(buffer, format="JPEG")
        (tmp_path / "matricula_01519_pg001.jpg").write_bytes(buffer.getvalue())
        assert downloader._is_already_downloaded(1) is True

    def test_is_already_downloaded_invalid_jpeg(self, tmp_path):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir=str(tmp_path)
        )
        downloader = MatriculaDownloader(config)

        # Invalid JPEG content should not count as already downloaded
        (tmp_path / "matricula_01519_pg001.jpg").write_bytes(b"fake")
        assert downloader._is_already_downloaded(1) is False

    @pytest.mark.asyncio
    async def test_verify_image_valid_jpeg(self, tmp_path):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir=str(tmp_path)
        )
        downloader = MatriculaDownloader(config)

        # Create a valid decodable JPEG
        import io as _io

        from PIL import Image

        img = Image.new("RGB", (1, 1), color="red")
        buffer = _io.BytesIO()
        img.save(buffer, format="JPEG")
        img_path = tmp_path / "matricula_01519_pg001.jpg"
        img_path.write_bytes(buffer.getvalue())

        valid = await downloader.verify_image(img_path)
        assert valid is True

    @pytest.mark.asyncio
    async def test_verify_image_invalid(self, tmp_path):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir=str(tmp_path)
        )
        downloader = MatriculaDownloader(config)

        # Not a JPEG
        img_path = tmp_path / "matricula_01519_pg001.jpg"
        img_path.write_bytes(b"not a jpeg")

        valid = await downloader.verify_image(img_path)
        assert valid is False

    @pytest.mark.asyncio
    async def test_verify_image_missing(self):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir="/tmp/nonexistent"
        )
        downloader = MatriculaDownloader(config)

        valid = await downloader.verify_image(
            Path("/tmp/nonexistent/matricula_01519_pg001.jpg")
        )
        assert valid is False

    @pytest.mark.asyncio
    async def test_download_page_skips_existing(self, tmp_path):
        config = MatriculaConfig(
            base_url="https://example.com/", book="01519", out_dir=str(tmp_path)
        )
        downloader = MatriculaDownloader(config)

        # Pre-create a valid JPEG file
        import io as _io

        from PIL import Image

        img = Image.new("RGB", (1, 1), color="red")
        buffer = _io.BytesIO()
        img.save(buffer, format="JPEG")
        img_path = tmp_path / "matricula_01519_pg001.jpg"
        img_path.write_bytes(buffer.getvalue())

        result = await downloader.download_page(1)
        assert result.success is True
        assert result.file_path == img_path
        assert result.file_size == len(buffer.getvalue())


class TestPageResult:
    """Tests for PageResult dataclass."""

    def test_success_result(self):
        result = PageResult(
            page_number=104,
            success=True,
            file_path=Path("/tmp/matricula_01519_pg104.jpg"),
            file_size=2089000,
            duration_seconds=5.5,
        )
        assert result.page_number == 104
        assert result.success is True
        assert result.file_size == 2089000

    def test_failure_result(self):
        result = PageResult(
            page_number=105,
            success=False,
            error="ImageNotFoundError: No image URL found",
        )
        assert result.page_number == 105
        assert result.success is False
        assert result.error == "ImageNotFoundError: No image URL found"
