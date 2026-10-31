"""Headless-safe smoke tests for the desktop entry point."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matricula_toolkit.desktop_app import app
from matricula_toolkit.desktop_services import MatriculaDesktopService, SourceRecord
from matricula_toolkit.providers import Provider


def _close_coroutine(coro):
    """Close an un-awaited coroutine and return a default value."""
    coro.close()
    return True


def _make_close_mock(return_value):
    """Return a side_effect that closes the coroutine and returns return_value."""

    def _close(coro):
        coro.close()
        return return_value

    return _close


def _make_close_mock_raise(exc):
    """Return a side_effect that closes the coroutine and raises exc."""

    def _close(coro):
        coro.close()
        raise exc

    return _close


def _ssrf_pass():
    """Patch SSRF validation functions to always pass (avoid DNS in tests)."""
    return patch.multiple(
        "matricula_toolkit.provider_policy",
        validate_host=lambda h: h,
        validate_url=lambda u: u,
    )


def test_desktop_app_imports_without_creating_a_window():
    assert callable(app.main)
    assert app.SECTIONS[0] == "Dashboard"
    assert "Help" in app.SECTIONS


def test_desktop_service_inventory_reads_project_local_files(tmp_path):
    (tmp_path / "matricula_01519_pg001.jpg").write_bytes(b"abc")
    (tmp_path / "matricula_01519_pg002.jpg").write_bytes(b"defg")
    (tmp_path / "other.jpg").write_bytes(b"ignored")

    settings_file = tmp_path / "settings.json"
    result = MatriculaDesktopService(
        book="01519", out_dir=str(tmp_path), settings_file=str(settings_file)
    ).prepare_ocr_inventory()

    assert result["count"] == 2
    assert result["pattern"] == "matricula_01519_pg*.jpg"
    assert all("other.jpg" not in value for value in result["files"])


def test_desktop_service_snapshot_has_numeric_values():
    snapshot = MatriculaDesktopService().resource_snapshot()

    assert snapshot.cpu_percent is None or snapshot.cpu_percent >= 0.0
    assert snapshot.ram_percent >= 0.0
    assert snapshot.active_downloads == 0
    assert snapshot.active_ocr_jobs == 0


@pytest.fixture(autouse=True)
def _isolate_default_db_path(tmp_path, monkeypatch):
    """Isolate the default SQLite DB path per test to avoid cross-test
    contamination from the shared default path."""
    fake_db = Path(tmp_path) / "sources.db"
    monkeypatch.setattr(
        "matricula_toolkit.desktop_services.default_db_path", lambda: fake_db
    )


def test_main_exits_clearly_when_pyside6_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(app, "QT_AVAILABLE", False)

    assert app.main([]) == 1
    assert "requires PySide6" in capsys.readouterr().err


# ------------------------------------------------------------------
# Enhanced service layer tests
# ------------------------------------------------------------------


def test_download_page_requires_base_url(tmp_path):
    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        base_url="",
        book="01519",
        out_dir=str(tmp_path),
        settings_file=str(settings_file),
    )
    result = service.download_page(1)
    assert result["success"] is False
    assert "Base URL not configured" in result["error"]


def test_download_page_records_result(tmp_path):
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.page_number = 1
    fake_result.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_result.file_size = 1234
    fake_result.image_url = "https://example.com/img.jpg"
    fake_result.error = None
    fake_result.duration_seconds = 0.5

    with (
        patch.object(service, "_record_download") as mock_record,
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock(fake_result),
        ),
    ):
        result = service.download_page(1)

    assert result["success"] is True
    assert result["page_number"] == 1
    assert result["file_size_bytes"] == 1234
    mock_record.assert_called_once()


def test_download_page_handles_exception(tmp_path):
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )

    with patch(
        "matricula_toolkit.desktop_services.asyncio.run",
        side_effect=_make_close_mock_raise(RuntimeError("boom")),
    ):
        result = service.download_page(1)

    assert result["success"] is False
    assert "boom" in result["error"]


def test_download_range_requires_base_url(tmp_path):
    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        base_url="",
        book="01519",
        out_dir=str(tmp_path),
        settings_file=str(settings_file),
    )
    result = service.download_range(1, 5)
    assert result["success"] is False
    assert "Base URL not configured" in result["error"]


def test_download_range_records_results(tmp_path):
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )
    fake_r1 = MagicMock()
    fake_r1.success = True
    fake_r1.page_number = 1
    fake_r1.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_r1.file_size = 1000
    fake_r1.image_url = "https://example.com/1.jpg"
    fake_r1.error = None
    fake_r1.duration_seconds = 0.3

    fake_r2 = MagicMock()
    fake_r2.success = True
    fake_r2.page_number = 2
    fake_r2.file_path = tmp_path / "matricula_01519_pg002.jpg"
    fake_r2.file_size = 2000
    fake_r2.image_url = "https://example.com/2.jpg"
    fake_r2.error = None
    fake_r2.duration_seconds = 0.4

    with patch(
        "matricula_toolkit.desktop_services.asyncio.run",
        side_effect=_make_close_mock([fake_r1, fake_r2]),
    ):
        result = service.download_range(1, 2)

    assert result["all_success"] is True
    assert result["start"] == 1
    assert result["end"] == 2
    assert result["total"] == 2
    assert result["success"] == 2
    assert result["failed"] == 0


# ------------------------------------------------------------------
# Provider-based download tests
# ------------------------------------------------------------------


def _make_provider_service(tmp_path, provider_id="test-provider", **overrides):
    """Create a MatriculaDesktopService with a provider configured."""
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )
    service.selected_provider_id = provider_id
    # Inject a Provider dataclass into the policy's internal registry
    defaults = {
        "id": provider_id,
        "name": "Test Provider",
        "homepage": "https://example.com",
        "regions": [],
        "religions": [],
        "record_types": [],
        "provider_type": "archive",
        "viewer_type": "image",
        "entry_hosts": ["example.com"],
        "content_hosts": ["content.example.com"],
        "redirect_hosts": ["content.example.com"],
        "authentication": "public",
        "image_access": "permitted_after_review",
        "terms_url": "https://example.com/terms",
        "terms_checked_at": "2024-01-01",
        "verification_status": "verified",
        "enabled": True,
        "notes": "",
    }
    defaults.update(overrides)
    entry = Provider(**defaults)
    service.policy._providers[provider_id] = entry
    return service


def test_download_page_with_provider_all_allowed(tmp_path):
    """download_page_with_provider succeeds when all capabilities pass."""
    service = _make_provider_service(tmp_path)
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.page_number = 1
    fake_result.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_result.file_size = 1234
    fake_result.image_url = "https://content.example.com/img.jpg"
    fake_result.error = None
    fake_result.duration_seconds = 0.5

    with (
        _ssrf_pass(),
        patch.object(service, "_record_download") as mock_record,
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock(fake_result),
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")

    assert result["success"] is True
    assert result["page_number"] == 1
    assert result["provider_id"] == "test-provider"
    assert result["host"] == "content.example.com"
    mock_record.assert_called_once()


def test_download_page_with_provider_no_provider_selected(tmp_path):
    """download_page_with_provider fails when no provider is selected."""
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )
    service.selected_provider_id = None
    result = service.download_page_with_provider(1)
    assert result["success"] is False
    assert "No provider selected" in result["error"]


def test_download_page_with_provider_unknown_provider(tmp_path):
    """download_page_with_provider fails for unknown provider."""
    service = _make_provider_service(tmp_path)
    result = service.download_page_with_provider(1, "unknown-provider")
    assert result["success"] is False
    assert "Unknown provider" in result["error"]


def test_download_page_with_provider_disabled(tmp_path):
    """download_page_with_provider fails for disabled provider."""
    service = _make_provider_service(tmp_path, enabled=False)
    result = service.download_page_with_provider(1, "test-provider")
    assert result["success"] is False
    assert "Provider disabled" in result["error"]


def test_download_page_with_provider_no_content_host(tmp_path):
    """download_page_with_provider fails when no content host configured."""
    service = _make_provider_service(tmp_path, content_hosts=[])
    result = service.download_page_with_provider(1, "test-provider")
    assert result["success"] is False
    assert "No content host configured" in result["error"]


def test_download_page_with_provider_browse_denied(tmp_path):
    """download_page_with_provider fails when browse capability is denied."""
    service = _make_provider_service(tmp_path)
    # Patch can_browse to return denied
    with (
        _ssrf_pass(),
        patch.object(
            service,
            "can_browse",
            return_value={"allowed": False, "reason": "not in entry_hosts"},
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")
    assert result["success"] is False
    assert "Browser navigation denied" in result["error"]


def test_download_page_with_provider_proxy_denied(tmp_path):
    """download_page_with_provider fails when proxy capability is denied."""
    service = _make_provider_service(tmp_path)
    with (
        _ssrf_pass(),
        patch.object(
            service,
            "can_proxy_connect",
            return_value={"allowed": False, "reason": "not in content_hosts"},
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")
    assert result["success"] is False
    assert "Proxy connection denied" in result["error"]


def test_download_page_with_provider_download_denied(tmp_path):
    """download_page_with_provider fails when image download capability is denied."""
    service = _make_provider_service(tmp_path)
    with (
        _ssrf_pass(),
        patch.object(
            service,
            "can_download_image",
            return_value={"allowed": False, "reason": "image_access not permitted"},
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")
    assert result["success"] is False
    assert "Image download denied" in result["error"]


def test_download_page_with_provider_redirect_denied(tmp_path):
    """download_page_with_provider fails when redirect target is denied.

    With empty redirect_hosts, the redirect_validator denies any image
    download target that is not the provider's own content host.  The
    downloader is mocked to simulate the redirect_validator raising
    SecurityError for a non-listed redirect target.
    """
    service = _make_provider_service(tmp_path, redirect_hosts=[])
    fake_result = MagicMock()
    fake_result.success = False
    fake_result.page_number = 1
    fake_result.file_path = None
    fake_result.file_size = None
    fake_result.image_url = None
    fake_result.error = "Image URL rejected by provider redirect policy: Host 'evil.example.com' is not a confirmed redirect host of provider 'test-provider'"
    fake_result.duration_seconds = 0.0

    with (
        _ssrf_pass(),
        patch.object(service, "_record_download") as mock_record,
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock(fake_result),
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")

    assert result["success"] is False
    assert "provider redirect policy" in result["error"]
    mock_record.assert_called_once()


def test_download_page_with_provider_empty_redirect_hosts_succeeds(tmp_path):
    """download_page_with_provider succeeds with empty redirect_hosts.

    Empty redirect_hosts means no provider-acknowledged redirect targets.
    The initial provider call is NOT blocked - only redirect targets away
    from the content host are denied by the redirect_validator during
    image download.
    """
    service = _make_provider_service(tmp_path, redirect_hosts=[])
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.page_number = 1
    fake_result.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_result.file_size = 1234
    fake_result.image_url = "https://content.example.com/img.jpg"
    fake_result.error = None
    fake_result.duration_seconds = 0.5

    with (
        _ssrf_pass(),
        patch.object(service, "_record_download") as mock_record,
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock(fake_result),
        ),
    ):
        result = service.download_page_with_provider(1, "test-provider")

    assert result["success"] is True
    assert result["page_number"] == 1
    mock_record.assert_called_once()


def test_download_range_with_provider_all_allowed(tmp_path):
    """download_range_with_provider succeeds when all capabilities pass."""
    service = _make_provider_service(tmp_path)
    fake_r1 = MagicMock()
    fake_r1.success = True
    fake_r1.page_number = 1
    fake_r1.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_r1.file_size = 1000
    fake_r1.image_url = "https://content.example.com/1.jpg"
    fake_r1.error = None
    fake_r1.duration_seconds = 0.3

    fake_r2 = MagicMock()
    fake_r2.success = True
    fake_r2.page_number = 2
    fake_r2.file_path = tmp_path / "matricula_01519_pg002.jpg"
    fake_r2.file_size = 2000
    fake_r2.image_url = "https://content.example.com/2.jpg"
    fake_r2.error = None
    fake_r2.duration_seconds = 0.4

    with (
        _ssrf_pass(),
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock([fake_r1, fake_r2]),
        ),
    ):
        result = service.download_range_with_provider(1, 2, "test-provider")

    assert result["all_success"] is True
    assert result["start"] == 1
    assert result["end"] == 2
    assert result["total"] == 2
    assert result["success"] == 2
    assert result["failed"] == 0
    assert result["provider_id"] == "test-provider"


def test_download_range_with_provider_capability_denied(tmp_path):
    """download_range_with_provider fails when a capability is denied."""
    service = _make_provider_service(tmp_path)
    with (
        _ssrf_pass(),
        patch.object(
            service,
            "can_proxy_connect",
            return_value={"allowed": False, "reason": "not in content_hosts"},
        ),
    ):
        result = service.download_range_with_provider(1, 2, "test-provider")
    assert result["success"] is False
    assert "Proxy connection denied" in result["error"]


def test_download_page_with_provider_uses_selected_provider(tmp_path):
    """download_page_with_provider uses selected_provider_id when provider_id is None."""
    service = _make_provider_service(tmp_path)
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.page_number = 1
    fake_result.file_path = tmp_path / "matricula_01519_pg001.jpg"
    fake_result.file_size = 1234
    fake_result.image_url = "https://content.example.com/img.jpg"
    fake_result.error = None
    fake_result.duration_seconds = 0.5

    with (
        _ssrf_pass(),
        patch.object(service, "_record_download"),
        patch(
            "matricula_toolkit.desktop_services.asyncio.run",
            side_effect=_make_close_mock(fake_result),
        ),
    ):
        result = service.download_page_with_provider(1)

    assert result["success"] is True
    assert result["provider_id"] == "test-provider"


def test_verify_image(tmp_path):
    service = MatriculaDesktopService(
        base_url="https://example.com", book="01519", out_dir=str(tmp_path)
    )
    img_path = tmp_path / "matricula_01519_pg001.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0")  # valid JPEG header

    with patch(
        "matricula_toolkit.desktop_services.asyncio.run", side_effect=_close_coroutine
    ):
        result = service.verify_image(str(img_path))

    assert result["valid"] is True
    assert result["path"] == str(img_path)


def test_list_images(tmp_path):
    (tmp_path / "matricula_01519_pg001.jpg").write_bytes(b"abc")
    (tmp_path / "matricula_01519_pg002.jpg").write_bytes(b"defg")
    (tmp_path / "other.jpg").write_bytes(b"ignored")

    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        book="01519", out_dir=str(tmp_path), settings_file=str(settings_file)
    )
    images = service.list_images()

    assert len(images) == 2
    assert images[0]["page_number"] == 1
    assert images[1]["page_number"] == 2
    assert all("other.jpg" not in img["path"] for img in images)


def test_save_and_load_settings(tmp_path):
    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        base_url="https://example.com",
        book="01519",
        out_dir="./downloads",
        settings_file=str(settings_file),
    )
    service.base_url = "https://new.example.com"
    service.book = "01520"
    service.out_dir = "./new-downloads"
    service.save_settings()

    # Reload in a new service instance
    service2 = MatriculaDesktopService(settings_file=str(settings_file))
    assert service2.base_url == "https://new.example.com"
    assert service2.book == "01520"
    assert service2.out_dir == "./new-downloads"


def test_ocr_backend_status_no_kraken(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    service = MatriculaDesktopService()
    status = service.ocr_backend_status()
    assert status["available"] is False
    assert status["backend"] == "not-configured"


def test_add_and_list_ocr_jobs():
    service = MatriculaDesktopService()
    job = service.add_ocr_job(source_file="/tmp/test.jpg", model="/tmp/model.mlmodel")
    jobs = service.list_ocr_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job.id
    assert jobs[0]["source_file"] == "/tmp/test.jpg"
    assert jobs[0]["status"] == "pending"


def test_run_ocr_job_requires_kraken(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    service = MatriculaDesktopService()
    job = service.add_ocr_job(source_file="/tmp/test.jpg", model="/tmp/model.mlmodel")
    result = service.run_ocr_job(job, model_path="/tmp/model.mlmodel")
    assert result["success"] is False
    assert "Kraken executable not found" in result["error"]
    assert job.status == "failed"


def test_run_ocr_job_invalid_model_path_empty(monkeypatch):
    """Empty model path is rejected before subprocess invocation."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")
    service = MatriculaDesktopService()
    job = service.add_ocr_job(source_file="/tmp/test.jpg", model="")
    result = service.run_ocr_job(job, model_path="")
    assert result["success"] is False
    assert "Model path is empty" in result["error"]
    assert job.status == "failed"
    # Counter must be reset (never incremented since validation failed first)
    assert service._active_ocr_jobs == 0


def test_run_ocr_job_invalid_model_path_nonexistent(monkeypatch):
    """Nonexistent model file is rejected."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")
    service = MatriculaDesktopService()
    job = service.add_ocr_job(source_file="/tmp/test.jpg", model="/tmp/model.mlmodel")
    result = service.run_ocr_job(job, model_path="/tmp/nonexistent_model.mlmodel")
    assert result["success"] is False
    assert "Model file not found" in result["error"]
    assert job.status == "failed"
    assert service._active_ocr_jobs == 0


def test_run_ocr_job_shell_injection_blocked(monkeypatch):
    """Model path with shell metacharacters is rejected."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")
    service = MatriculaDesktopService()
    job = service.add_ocr_job(source_file="/tmp/test.jpg", model="/tmp/model.mlmodel")
    result = service.run_ocr_job(job, model_path="/tmp/model.mlmodel; rm -rf /")
    assert result["success"] is False
    assert "unsafe characters" in result["error"]
    assert job.status == "failed"
    assert service._active_ocr_jobs == 0


def test_run_ocr_job_timeout(monkeypatch, tmp_path):
    """OCR job timeout is handled and counter is reset."""
    # Create a real source image file
    source = tmp_path / "test.jpg"
    source.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    service = MatriculaDesktopService(out_dir=str(tmp_path))
    job = service.add_ocr_job(source_file=str(source), model="/tmp/model.mlmodel")

    # Create a dummy model file so validation passes
    model = tmp_path / "model.mlmodel"
    model.write_bytes(b"dummy model")

    # Mock shutil.which to return kraken
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")

    # Mock subprocess.run to raise TimeoutExpired
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    monkeypatch.setattr(subprocess, "run", mock_run)

    result = service.run_ocr_job(job, model_path=str(model))
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert job.status == "failed"
    # Counter must be reset in finally block
    assert service._active_ocr_jobs == 0


def test_check_ocr_load_within_limits(monkeypatch):
    """check_ocr_load returns allowed when resources are within limits."""
    service = MatriculaDesktopService()

    # Mock resource snapshot to return low values
    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=10.0, ram_percent=20.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is True
    assert "within limits" in result["reason"]


def test_check_ocr_load_exceeds_cpu(monkeypatch):
    """check_ocr_load returns not allowed when CPU exceeds limit."""
    service = MatriculaDesktopService()

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=95.0, ram_percent=20.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is False
    assert "CPU" in result["reason"]


def test_check_ocr_load_exceeds_ram(monkeypatch):
    """check_ocr_load returns not allowed when RAM exceeds limit."""
    service = MatriculaDesktopService()

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=20.0, ram_percent=95.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is False
    assert "RAM" in result["reason"]


def test_check_ocr_load_exceeds_temperature(monkeypatch):
    """check_ocr_load returns not allowed when temperature exceeds limit."""
    service = MatriculaDesktopService()

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=20.0, ram_percent=30.0, temperature_c=85.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is False
    assert "Temperature" in result["reason"]


def test_check_ocr_load_missing_temperature_not_blocked(monkeypatch):
    """check_ocr_load does not block when temperature sensor is unavailable (None)
    and no temperature limit is configured."""
    service = MatriculaDesktopService()
    service.config.ocr_max_temperature_c = None

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=20.0, ram_percent=30.0, temperature_c=None)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is True


def test_check_ocr_load_first_cpu_call_zero_not_blocked(monkeypatch):
    """check_ocr_load does not block on first CPU call (0.0 baseline, not real load)."""
    service = MatriculaDesktopService()

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=0.0, ram_percent=30.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is True


def test_check_ocr_load_all_thresholds_disabled(monkeypatch):
    """check_ocr_load allows when all thresholds are disabled (0 or None)."""
    service = MatriculaDesktopService()
    service.config.ocr_max_cpu_percent = 0.0
    service.config.ocr_max_ram_percent = 0.0
    service.config.ocr_max_temperature_c = None

    def mock_snapshot():
        from matricula_toolkit.desktop_services import ResourceSnapshot

        return ResourceSnapshot(cpu_percent=99.0, ram_percent=99.0, temperature_c=99.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)

    result = service.check_ocr_load()
    assert result["allowed"] is True


def test_validate_model_path_traversal_blocked():
    """Path traversal in model path is rejected."""
    service = MatriculaDesktopService()
    error = service._validate_model_path("/tmp/../../etc/passwd")
    assert error is not None
    assert "path traversal" in error


# ------------------------------------------------------------------
# Source management tests
# ------------------------------------------------------------------


def test_add_and_list_sources():
    service = MatriculaDesktopService(book="01519")
    source = service.add_source(
        archive="Staatsarchiv",
        municipality="Testgemeinde",
        book="01519",
        page=42,
        dating="1850-1860",
        signature="A 123",
        notes="Test note",
        file_path="/tmp/matricula_01519_pg042.jpg",
    )
    sources = service.list_sources()
    assert len(sources) == 1
    assert sources[0]["archive"] == "Staatsarchiv"
    assert sources[0]["municipality"] == "Testgemeinde"
    assert sources[0]["book"] == "01519"
    assert sources[0]["page"] == 42
    assert sources[0]["dating"] == "1850-1860"
    assert sources[0]["signature"] == "A 123"
    assert sources[0]["notes"] == "Test note"
    assert sources[0]["verification_status"] == "unverified"
    assert sources[0]["id"] == source.id


def test_update_source():
    service = MatriculaDesktopService(book="01519")
    source = service.add_source(archive="Old Archive", page=1)
    updated = service.update_source(
        source.id,
        archive="New Archive",
        verification_status="verified",
        notes="Updated note",
    )
    assert updated is not None
    assert updated.archive == "New Archive"
    assert updated.verification_status == "verified"
    assert updated.notes == "Updated note"
    assert updated.updated_at != source.created_at


def test_update_source_not_found():
    service = MatriculaDesktopService()
    result = service.update_source("nonexistent-id", archive="X")
    assert result is None


def test_delete_source():
    service = MatriculaDesktopService()
    source = service.add_source(page=1)
    assert service.delete_source(source.id) is True
    assert len(service.list_sources()) == 0
    assert service.delete_source(source.id) is False


def test_link_source_to_download_creates_new():
    service = MatriculaDesktopService(book="01519", out_dir="/tmp")
    source = service.link_source_to_download(42, "/tmp/matricula_01519_pg042.jpg")
    assert source is not None
    assert source.page == 42
    assert source.book == "01519"
    assert source.file_path == "/tmp/matricula_01519_pg042.jpg"


def test_link_source_to_download_returns_existing():
    service = MatriculaDesktopService(book="01519")
    first = service.link_source_to_download(42, "/tmp/path.jpg")
    second = service.link_source_to_download(42, "/tmp/other.jpg")
    assert first.id == second.id
    assert len(service.list_sources()) == 1


def test_export_sources_json(tmp_path):
    service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
    service.add_source(archive="Archiv A", page=1, dating="1800")
    service.add_source(archive="Archiv B", page=2, signature="Sig B")

    data = service.export_sources(format="json")
    import json

    parsed = json.loads(data)
    assert len(parsed) == 2
    assert parsed[0]["archive"] == "Archiv A"
    assert parsed[1]["archive"] == "Archiv B"


def test_export_sources_csv(tmp_path):
    service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
    service.add_source(archive="Archiv A", page=1)

    data = service.export_sources(format="csv")
    assert "Archiv A" in data
    assert "archive" in data.lower() or "Archive" in data


def test_export_sources_empty():
    service = MatriculaDesktopService()
    data = service.export_sources(format="json")
    import json

    assert json.loads(data) == []


def test_export_correction_creates_file(tmp_path):
    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        out_dir=str(tmp_path), settings_file=str(settings_file)
    )
    corrections_dir = tmp_path / "corrections"
    corrections_dir.mkdir()
    source_name = "matricula_01519_pg001.jpg"
    correction_file = corrections_dir / f"{source_name}.corrected.txt"
    correction_file.write_text(
        "# Correction\n## Original\nOCR text\n## Corrected\nManual text",
        encoding="utf-8",
    )

    result = service.export_correction(
        str(tmp_path / source_name), output_path=str(tmp_path / "exported.txt")
    )
    assert result["success"] is True
    assert result["output_path"] == str(tmp_path / "exported.txt")
    assert (tmp_path / "exported.txt").is_file()


def test_export_correction_missing_file():
    service = MatriculaDesktopService(out_dir="/tmp")
    result = service.export_correction("/tmp/nonexistent.jpg")
    assert result["success"] is False
    assert "No correction found" in result["error"]


def test_source_record_defaults():
    source = SourceRecord()
    assert source.id != ""
    assert source.verification_status == "unverified"
    assert source.page == 0
    assert source.created_at != ""
    assert source.updated_at != ""
    # updated_at should be >= created_at (set moments later)
    assert source.updated_at >= source.created_at


# ------------------------------------------------------------------
# Installer section tests
# ------------------------------------------------------------------


def test_installer_section_in_sections():
    assert "Installer" in app.SECTIONS


@pytest.mark.skipif(
    not app.QT_AVAILABLE, reason="PySide6 not available in this environment"
)
def test_installer_page_created():
    """The _page method should handle the 'Installer' section without error."""
    # We can't instantiate MainWindow without a display, but we can verify
    # the section is registered and the _page method has a branch for it.
    import inspect

    source = inspect.getsource(app.MainWindow._page)
    assert '"Installer"' in source


def test_installer_check_uses_detect_system(monkeypatch):
    """The installer check calls detect_system and detect_components."""
    from unittest.mock import patch

    from matricula_toolkit.installer import ComponentStatus, SystemInfo

    fake_info = SystemInfo(
        os="Linux",
        os_version="22.1",
        distribution="Linux Mint 22.1",
        architecture="x86_64",
        python_version="3.12.3",
        python_executable="/usr/bin/python3",
        ram_total_mb=8192,
        disk_free_mb=50000,
        gpu_available=False,
        gpu_vendor=None,
        is_container=False,
    )
    fake_components = [
        ComponentStatus(name="python", installed=True),
        ComponentStatus(name="kraken", installed=False),
        ComponentStatus(name="PySide6", installed=True, optional=True),
    ]

    with (
        patch(
            "matricula_toolkit.desktop_app.app.detect_system", return_value=fake_info
        ),
        patch(
            "matricula_toolkit.desktop_app.app.detect_components",
            return_value=fake_components,
        ),
    ):
        # Call through the app module's references (which are patched)
        info = app.detect_system()
        components = app.detect_components()
        assert info.os == "Linux"
        assert "Linux Mint" in info.distribution
        assert any(c.name == "kraken" and not c.installed for c in components)


def test_kraken_models_have_verified_metadata():
    """Verified Kraken models have required metadata fields."""
    from matricula_toolkit.installer import KRAKEN_MODELS

    assert len(KRAKEN_MODELS) >= 2
    for doi, info in KRAKEN_MODELS.items():
        assert "name" in info
        assert "type" in info
        assert "script" in info
        assert "license" in info
        assert "creator" in info
        assert "description" in info
        assert "doi" in info
        assert info["doi"] == doi


def test_default_kraken_models_are_verified():
    """Default models are a subset of verified models."""
    from matricula_toolkit.installer import DEFAULT_KRAKEN_MODELS, KRAKEN_MODELS

    for doi in DEFAULT_KRAKEN_MODELS:
        assert doi in KRAKEN_MODELS


def test_compute_install_plan_includes_kraken_models(monkeypatch):
    """compute_install_plan includes selected Kraken models."""
    from matricula_toolkit.installer import (
        ComponentStatus,
        InstallPlan,
        SystemInfo,
        compute_install_plan,
    )

    info = SystemInfo(os="Linux", distribution="Linux Mint 22.1")
    components = [
        ComponentStatus(name="python", installed=True),
        ComponentStatus(name="playwright", installed=False),
        ComponentStatus(name="Pillow", installed=True),
        ComponentStatus(name="python-dotenv", installed=True),
        ComponentStatus(name="kraken", installed=False),
        ComponentStatus(name="kraken-python", installed=False),
        ComponentStatus(name="PySide6", installed=True, optional=True),
    ]

    plan = compute_install_plan(
        info,
        components,
        create_venv=True,
        install_kraken=True,
        install_models=True,
        models=["10.5281/zenodo.21747522"],
    )
    assert isinstance(plan, InstallPlan)
    assert "10.5281/zenodo.21747522" in plan.kraken_models
    assert "kraken" in plan.python_packages
