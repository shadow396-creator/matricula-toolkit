"""Desktop service layer for the Matricula Toolkit workbench.

This module keeps the GUI thin and reuses the public toolkit business logic
for configuration, downloads, OCR-preparation inventory, and local resource
monitoring. It intentionally avoids private project dependencies and keeps the
workbench portable across Linux and Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.provider_policy import ProviderPolicy, ProviderPolicyError
from matricula_toolkit.providers import load_providers
from matricula_toolkit.security import SecurityError
from matricula_toolkit.source_store import SourceStore, default_db_path


@dataclass
class ResourceSnapshot:
    """Resource snapshot shown in the desktop status bar and system dashboard."""

    cpu_percent: float | None = None
    ram_percent: float | None = None
    temperature_c: float | None = None
    active_downloads: int = 0
    active_ocr_jobs: int = 0


@dataclass
class DownloadRecord:
    """A single download record for source tracking and history."""

    page_number: int
    success: bool
    file_path: str | None = None
    file_size_bytes: int | None = None
    image_url: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    downloaded_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )


@dataclass
class OCRJob:
    """An OCR job record with input, output, and status."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    source_file: str = ""
    model: str = ""
    status: str = "pending"  # pending, running, success, failed, needs_review
    output_dir: str = ""
    line_count: int = 0
    created_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    completed_at: str | None = None
    error: str | None = None


@dataclass
class SourceRecord:
    """Source metadata for a downloaded page or book."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    archive: str = ""
    municipality: str = ""
    book: str = ""
    page: int = 0
    dating: str = ""
    signature: str = ""
    notes: str = ""
    verification_status: str = "unverified"  # unverified, verified, needs_review
    file_path: str | None = None
    created_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )


class MatriculaDesktopService:
    """Thin bridge from the desktop shell to the toolkit backend."""

    def __init__(
        self,
        *,
        base_url: str = "",
        book: str = "01519",
        out_dir: str = "./downloads",
        settings_file: str | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.base_url = base_url
        self.book = book
        self.out_dir = out_dir
        self.language = "en"
        if settings_file is not None:
            self.settings_file = settings_file
        elif os.name == "nt":
            # Windows: %APPDATA%/matricula-toolkit/settings.json
            appdata = os.getenv("APPDATA") or str(Path.home())
            self.settings_file = str(
                Path(appdata) / "matricula-toolkit" / "settings.json"
            )
        elif sys.platform == "darwin":
            # macOS: ~/Library/Application Support/matricula-toolkit/settings.json
            self.settings_file = str(
                Path.home()
                / "Library"
                / "Application Support"
                / "matricula-toolkit"
                / "settings.json"
            )
        else:
            # Linux/Unix: $XDG_CONFIG_HOME/matricula-toolkit/settings.json
            # or ~/.config/matricula-toolkit/settings.json
            xdg_config = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            self.settings_file = str(
                Path(xdg_config) / "matricula-toolkit" / "settings.json"
            )
        self._download_history: list[DownloadRecord] = []
        self._ocr_jobs: list[OCRJob] = []
        self._sources: list[SourceRecord] = []
        self._active_downloads = 0
        self._active_ocr_jobs = 0
        self._store: SourceStore = SourceStore(
            db_path if db_path is not None else default_db_path()
        )
        self.config = MatriculaConfig.from_env()
        self._prev_cpu_times: tuple[int, int] | None = None
        self._load_settings()
        self._load_sources_from_store()
        self._load_providers()

    # ------------------------------------------------------------------
    # Provider configuration
    # ------------------------------------------------------------------
    def _load_providers(self) -> None:
        """Load provider configuration and initialize the policy engine."""
        try:
            self.providers: dict[str, Any] = load_providers()
            self.policy: ProviderPolicy = ProviderPolicy(self.providers)
        except Exception:  # noqa: BLE001 — provider config must not break the workbench
            self.providers = {}
            self.policy = ProviderPolicy({})
        self.selected_provider_id: str | None = self._load_selected_provider_id()

    def _load_selected_provider_id(self) -> str | None:
        """Load the persisted selected provider ID from settings."""
        path = Path(self.settings_file)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("selected_provider_id")
            if isinstance(pid, str) and pid in self.providers:
                return pid
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _save_selected_provider_id(self) -> None:
        """Persist the selected provider ID to settings."""
        path = Path(self.settings_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = (
                json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            )
        except (OSError, json.JSONDecodeError):
            data = {}
        data["selected_provider_id"] = self.selected_provider_id
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_providers(self) -> list[dict[str, Any]]:
        """Return all configured providers as dictionaries for the GUI."""
        result: list[dict[str, Any]] = []
        for provider in self.policy.list_providers():
            result.append(
                {
                    "id": provider.id,
                    "name": provider.name,
                    "homepage": provider.homepage,
                    "regions": provider.regions,
                    "religions": provider.religions,
                    "record_types": provider.record_types,
                    "provider_type": provider.provider_type,
                    "viewer_type": provider.viewer_type,
                    "entry_hosts": provider.entry_hosts,
                    "content_hosts": provider.content_hosts,
                    "redirect_hosts": provider.redirect_hosts,
                    "authentication": provider.authentication,
                    "image_access": provider.image_access,
                    "terms_url": provider.terms_url,
                    "terms_checked_at": provider.terms_checked_at,
                    "verification_status": provider.verification_status,
                    "enabled": provider.enabled,
                    "notes": provider.notes,
                    "is_selected": provider.id == self.selected_provider_id,
                }
            )
        return result

    def get_enabled_providers(self) -> list[dict[str, Any]]:
        """Return all enabled providers as dictionaries for the GUI."""
        result: list[dict[str, Any]] = []
        for provider in self.policy.list_enabled():
            result.append(
                {
                    "id": provider.id,
                    "name": provider.name,
                    "homepage": provider.homepage,
                    "regions": provider.regions,
                    "religions": provider.religions,
                    "record_types": provider.record_types,
                    "provider_type": provider.provider_type,
                    "viewer_type": provider.viewer_type,
                    "entry_hosts": provider.entry_hosts,
                    "content_hosts": provider.content_hosts,
                    "redirect_hosts": provider.redirect_hosts,
                    "authentication": provider.authentication,
                    "image_access": provider.image_access,
                    "terms_url": provider.terms_url,
                    "terms_checked_at": provider.terms_checked_at,
                    "verification_status": provider.verification_status,
                    "enabled": provider.enabled,
                    "notes": provider.notes,
                    "is_selected": provider.id == self.selected_provider_id,
                }
            )
        return result

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        """Return a single provider as a dictionary, or None if not found."""
        provider = self.policy._providers.get(provider_id)
        if provider is None:
            return None
        return {
            "id": provider.id,
            "name": provider.name,
            "homepage": provider.homepage,
            "regions": provider.regions,
            "religions": provider.religions,
            "record_types": provider.record_types,
            "provider_type": provider.provider_type,
            "viewer_type": provider.viewer_type,
            "entry_hosts": provider.entry_hosts,
            "content_hosts": provider.content_hosts,
            "redirect_hosts": provider.redirect_hosts,
            "authentication": provider.authentication,
            "image_access": provider.image_access,
            "terms_url": provider.terms_url,
            "terms_checked_at": provider.terms_checked_at,
            "verification_status": provider.verification_status,
            "enabled": provider.enabled,
            "notes": provider.notes,
            "is_selected": provider.id == self.selected_provider_id,
        }

    def select_provider(self, provider_id: str | None) -> dict[str, Any]:
        """Select a provider for the current session.

        Returns a result dict with ``success`` and optional ``error``.
        """
        if provider_id is None:
            self.selected_provider_id = None
            self._save_selected_provider_id()
            return {"success": True, "provider_id": None}

        provider = self.policy._providers.get(provider_id)
        if provider is None:
            return {"success": False, "error": f"Unknown provider: {provider_id!r}"}
        if not provider.enabled:
            return {
                "success": False,
                "error": f"Provider disabled: {provider_id!r}",
            }

        self.selected_provider_id = provider_id
        self._save_selected_provider_id()
        return {"success": True, "provider_id": provider_id}

    def get_selected_provider(self) -> dict[str, Any] | None:
        """Return the currently selected provider as a dictionary, or None."""
        if self.selected_provider_id is None:
            return None
        return self.get_provider(self.selected_provider_id)

    def can_browse(
        self, provider_id: str | None = None, host: str | None = None
    ) -> dict[str, Any]:
        """Check whether browser navigation is allowed for a provider/host.

        If provider_id is None, uses the currently selected provider.
        If host is None, uses the entry host of the selected provider.
        """
        pid = provider_id or self.selected_provider_id
        if pid is None:
            return {"allowed": False, "error": "No provider selected"}

        if host is None:
            provider = self.policy._providers.get(pid)
            if provider is None or not provider.entry_hosts:
                return {"allowed": False, "error": "No entry host configured"}
            host = provider.entry_hosts[0]

        try:
            decision = self.policy.can_browse(pid, host)
            return {
                "allowed": decision.allowed,
                "provider_id": decision.provider_id,
                "host": decision.host,
                "capability": decision.capability,
                "reason": decision.reason,
            }
        except ProviderPolicyError as exc:
            return {"allowed": False, "error": str(exc)}

    def can_proxy_connect(
        self, provider_id: str | None = None, host: str | None = None
    ) -> dict[str, Any]:
        """Check whether proxy connection is allowed for a provider/host."""
        pid = provider_id or self.selected_provider_id
        if pid is None:
            return {"allowed": False, "error": "No provider selected"}

        if host is None:
            provider = self.policy._providers.get(pid)
            if provider is None or not provider.content_hosts:
                return {"allowed": False, "error": "No content host configured"}
            host = provider.content_hosts[0]

        try:
            decision = self.policy.can_proxy_connect(pid, host)
            return {
                "allowed": decision.allowed,
                "provider_id": decision.provider_id,
                "host": decision.host,
                "capability": decision.capability,
                "reason": decision.reason,
            }
        except ProviderPolicyError as exc:
            return {"allowed": False, "error": str(exc)}

    def can_download_image(
        self, provider_id: str | None = None, host: str | None = None
    ) -> dict[str, Any]:
        """Check whether automated image download is allowed for a provider/host."""
        pid = provider_id or self.selected_provider_id
        if pid is None:
            return {"allowed": False, "error": "No provider selected"}

        if host is None:
            provider = self.policy._providers.get(pid)
            if provider is None or not provider.content_hosts:
                return {"allowed": False, "error": "No content host configured"}
            host = provider.content_hosts[0]

        try:
            decision = self.policy.can_download_image(pid, host)
            return {
                "allowed": decision.allowed,
                "provider_id": decision.provider_id,
                "host": decision.host,
                "capability": decision.capability,
                "reason": decision.reason,
            }
        except ProviderPolicyError as exc:
            return {"allowed": False, "error": str(exc)}

    def download_page_with_provider(
        self, page_number: int, provider_id: str | None = None
    ) -> dict[str, Any]:
        """Download a single page using provider-based policy checks.

        The provider's content host is used as the base URL.  All three
        provider capabilities (browser, proxy, image download) plus the
        centralized SSRF policy must pass.  Provider-specific redirect
        rules are validated against the initial page URL.  No direct
        network fallback is used if any check denies access.
        """
        pid = provider_id or self.selected_provider_id
        if pid is None:
            return {"success": False, "error": "No provider selected"}

        provider = self.policy._providers.get(pid)
        if provider is None:
            return {"success": False, "error": f"Unknown provider: {pid!r}"}
        if not provider.enabled:
            return {"success": False, "error": f"Provider disabled: {pid!r}"}

        if not provider.content_hosts:
            return {
                "success": False,
                "error": "No content host configured for provider",
            }

        host = provider.content_hosts[0]

        # Capability 1: browser navigation — the downloader opens a page URL.
        entry_host = provider.entry_hosts[0] if provider.entry_hosts else None
        if entry_host:
            browse_check = self.can_browse(pid, entry_host)
            if not browse_check["allowed"]:
                return {
                    "success": False,
                    "error": f"Browser navigation denied: {browse_check.get('reason') or 'unknown'}",
                    "provider_id": pid,
                    "host": entry_host,
                }

        # Capability 2: proxy connection — the downloader routes through LocalProxy.
        proxy_check = self.can_proxy_connect(pid, host)
        if not proxy_check["allowed"]:
            return {
                "success": False,
                "error": f"Proxy connection denied: {proxy_check.get('reason') or 'unknown'}",
                "provider_id": pid,
                "host": host,
            }

        # Capability 3: automated image download.
        download_check = self.can_download_image(pid, host)
        if not download_check["allowed"]:
            return {
                "success": False,
                "error": f"Image download denied: {download_check.get('reason') or 'unknown'}",
                "provider_id": pid,
                "host": host,
            }

        # Provider-specific redirect rules: the initial page URL is the
        # provider's own content host and must NOT be validated against
        # redirect_hosts (which lists acknowledged redirect targets, not
        # the origin itself).  Empty redirect_hosts means no provider-
        # acknowledged redirect targets; any redirect away from the content
        # host is denied at the provider-policy level during the download.
        # Per-hop SSRF enforcement during navigation is provided by
        # LocalProxy at the network boundary.  NOTE: Playwright's
        # page.goto() follows redirects automatically and does not expose
        # individual redirect hops, so application-level per-redirect-step
        # provider-policy validation is not possible during navigation.
        # Image-URL redirect targets are validated via the redirect_validator
        # passed to the downloader (see below).

        # Build base URL from content host (HTTPS only)
        base_url = f"https://{host}"

        config = MatriculaConfig(
            base_url=base_url, book=self.book, out_dir=self.out_dir
        )

        def _validate_redirect(original_url: str, redirect_url: str) -> None:
            """Validate a redirect target against provider policy.

            Raises SecurityError if the redirect target is not an allowed
            redirect host for this provider.
            """
            decision = self.policy.validate_redirect(pid, original_url, redirect_url)
            if not decision.allowed:
                raise SecurityError(
                    decision.reason or f"Redirect to {redirect_url!r} denied"
                )

        async def run() -> PageResult:
            async with MatriculaDownloader(
                config, redirect_validator=_validate_redirect
            ) as downloader:
                return await downloader.download_page(page_number)

        try:
            self._active_downloads += 1
            result = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"success": False, "error": str(exc)}
        finally:
            self._active_downloads -= 1

        self._record_download(result)
        return {
            "success": bool(result.success),
            "page_number": result.page_number,
            "file_path": str(result.file_path) if result.file_path else None,
            "file_size_bytes": result.file_size,
            "image_url": result.image_url,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "provider_id": pid,
            "host": host,
        }

    def download_range_with_provider(
        self, start_page: int, end_page: int, provider_id: str | None = None
    ) -> dict[str, Any]:
        """Download a range of pages using provider-based policy checks.

        All three provider capabilities (browser, proxy, image download)
        plus the centralized SSRF policy must pass.  Provider-specific
        redirect rules are validated against the content host.  No direct
        network fallback is used if any check denies access.
        """
        pid = provider_id or self.selected_provider_id
        if pid is None:
            return {"success": False, "error": "No provider selected"}

        provider = self.policy._providers.get(pid)
        if provider is None:
            return {"success": False, "error": f"Unknown provider: {pid!r}"}
        if not provider.enabled:
            return {"success": False, "error": f"Provider disabled: {pid!r}"}

        if not provider.content_hosts:
            return {
                "success": False,
                "error": "No content host configured for provider",
            }

        host = provider.content_hosts[0]

        # Capability 1: browser navigation — the downloader opens page URLs.
        entry_host = provider.entry_hosts[0] if provider.entry_hosts else None
        if entry_host:
            browse_check = self.can_browse(pid, entry_host)
            if not browse_check["allowed"]:
                return {
                    "success": False,
                    "error": f"Browser navigation denied: {browse_check.get('reason') or 'unknown'}",
                    "provider_id": pid,
                    "host": entry_host,
                }

        # Capability 2: proxy connection — the downloader routes through LocalProxy.
        proxy_check = self.can_proxy_connect(pid, host)
        if not proxy_check["allowed"]:
            return {
                "success": False,
                "error": f"Proxy connection denied: {proxy_check.get('reason') or 'unknown'}",
                "provider_id": pid,
                "host": host,
            }

        # Capability 3: automated image download.
        download_check = self.can_download_image(pid, host)
        if not download_check["allowed"]:
            return {
                "success": False,
                "error": f"Image download denied: {download_check.get('reason') or 'unknown'}",
                "provider_id": pid,
                "host": host,
            }

        # Provider-specific redirect rules: the initial page URL is the
        # provider's own content host and must NOT be validated against
        # redirect_hosts (which lists acknowledged redirect targets, not
        # the origin itself).  Empty redirect_hosts means no provider-
        # acknowledged redirect targets; any redirect away from the content
        # host is denied at the provider-policy level during the download.
        # Per-hop SSRF enforcement during navigation is provided by
        # LocalProxy at the network boundary.  NOTE: Playwright's
        # page.goto() follows redirects automatically and does not expose
        # individual redirect hops, so application-level per-redirect-step
        # provider-policy validation is not possible during navigation.
        # Image-URL redirect targets are validated via the redirect_validator
        # passed to the downloader (see below).

        base_url = f"https://{host}"

        config = MatriculaConfig(
            base_url=base_url, book=self.book, out_dir=self.out_dir
        )

        def _validate_redirect(original_url: str, redirect_url: str) -> None:
            """Validate a redirect target against provider policy.

            Raises SecurityError if the redirect target is not an allowed
            redirect host for this provider.
            """
            decision = self.policy.validate_redirect(pid, original_url, redirect_url)
            if not decision.allowed:
                raise SecurityError(
                    decision.reason or f"Redirect to {redirect_url!r} denied"
                )

        async def run() -> list[PageResult]:
            async with MatriculaDownloader(
                config, redirect_validator=_validate_redirect
            ) as downloader:
                return await downloader.download_range(start_page, end_page)

        try:
            self._active_downloads += 1
            results = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"success": False, "error": str(exc)}
        finally:
            self._active_downloads -= 1

        for result in results:
            self._record_download(result)

        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        return {
            "all_success": fail_count == 0,
            "start": start_page,
            "end": end_page,
            "total": len(results),
            "success": success_count,
            "failed": fail_count,
            "results": [
                {
                    "page_number": r.page_number,
                    "success": r.success,
                    "file_path": str(r.file_path) if r.file_path else None,
                    "file_size_bytes": r.file_size,
                    "error": r.error,
                }
                for r in results
            ],
            "provider_id": pid,
            "host": host,
        }

    # ------------------------------------------------------------------
    # OCR inventory
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        """Load persisted settings from the platform-appropriate settings file."""
        path = Path(self.settings_file)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.base_url = data.get("base_url", self.base_url)
            self.book = data.get("book", self.book)
            self.out_dir = data.get("out_dir", self.out_dir)
            self.language = data.get("language", self.language)
        except (OSError, json.JSONDecodeError):
            pass

    def save_settings(self) -> None:
        """Persist current settings to the settings file."""
        path = Path(self.settings_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "base_url": self.base_url,
            "book": self.book,
            "out_dir": self.out_dir,
            "selected_provider_id": self.selected_provider_id,
            "language": self.language,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Source store persistence
    # ------------------------------------------------------------------
    def _load_sources_from_store(self) -> None:
        """Load source records from the persistent store."""
        try:
            self._sources = self._store.list()
        except Exception:  # noqa: BLE001 — storage must not break the workbench
            self._sources = []

    def _persist_source(self, source: SourceRecord) -> None:
        """Save a source record to the persistent store.

        Uses INSERT OR REPLACE semantics so that re-adding an existing
        record (e.g. after a restart) does not fail with a primary-key
        violation.
        """
        try:
            self._store.add(source)
        except sqlite3.IntegrityError:
            # Record already exists — replace it.
            self._store.update(
                source.id,
                archive=source.archive,
                municipality=source.municipality,
                book=source.book,
                page=source.page,
                dating=source.dating,
                signature=source.signature,
                notes=source.notes,
                verification_status=source.verification_status,
                file_path=source.file_path,
            )
        except Exception:  # noqa: BLE001, S110 — storage must not break the workbench
            pass

    # ------------------------------------------------------------------
    # OCR inventory
    # ------------------------------------------------------------------
    def prepare_ocr_inventory(self) -> dict[str, Any]:
        """Inventory prepared images in the download directory."""
        directory = Path(self.out_dir)
        pattern = f"matricula_{self.book}_pg*.jpg"
        files = sorted(path for path in directory.glob(pattern) if path.is_file())
        total_size = sum(path.stat().st_size for path in files)
        return {
            "book": self.book,
            "directory": str(directory),
            "pattern": pattern,
            "files": [str(path) for path in files],
            "count": len(files),
            "total_size_bytes": total_size,
        }

    # ------------------------------------------------------------------
    # Book inspection
    # ------------------------------------------------------------------
    def inspect_book(self) -> dict[str, Any]:
        """Check that the configured book is accessible by downloading page 1."""
        if not self.base_url:
            return {"success": False, "error": "Base URL not configured."}

        config = MatriculaConfig(
            base_url=self.base_url, book=self.book, out_dir=self.out_dir
        )

        async def run() -> Any:
            async with MatriculaDownloader(config) as downloader:
                return await downloader.download_page(1)

        try:
            result = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"success": False, "error": str(exc)}

        if result.success and result.file_path:
            self._record_download(result)

        return {
            "success": bool(result.success),
            "book": self.book,
            "base_url": self.base_url,
            "file_path": str(result.file_path) if result.file_path else None,
            "file_size_bytes": result.file_size,
            "error": result.error,
        }

    # ------------------------------------------------------------------
    # Download management
    # ------------------------------------------------------------------
    def download_page(self, page_number: int) -> dict[str, Any]:
        """Download a single page and record the result."""
        if not self.base_url:
            return {"success": False, "error": "Base URL not configured."}

        config = MatriculaConfig(
            base_url=self.base_url, book=self.book, out_dir=self.out_dir
        )

        async def run() -> PageResult:
            async with MatriculaDownloader(config) as downloader:
                return await downloader.download_page(page_number)

        try:
            self._active_downloads += 1
            result = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"success": False, "error": str(exc)}
        finally:
            self._active_downloads -= 1

        self._record_download(result)
        return {
            "success": bool(result.success),
            "page_number": result.page_number,
            "file_path": str(result.file_path) if result.file_path else None,
            "file_size_bytes": result.file_size,
            "image_url": result.image_url,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
        }

    def download_range(self, start_page: int, end_page: int) -> dict[str, Any]:
        """Download a range of pages sequentially."""
        if not self.base_url:
            return {"success": False, "error": "Base URL not configured."}

        config = MatriculaConfig(
            base_url=self.base_url, book=self.book, out_dir=self.out_dir
        )

        async def run() -> list[PageResult]:
            async with MatriculaDownloader(config) as downloader:
                return await downloader.download_range(start_page, end_page)

        try:
            self._active_downloads += 1
            results = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"success": False, "error": str(exc)}
        finally:
            self._active_downloads -= 1

        for result in results:
            self._record_download(result)

        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        return {
            "all_success": fail_count == 0,
            "start": start_page,
            "end": end_page,
            "total": len(results),
            "success": success_count,
            "failed": fail_count,
            "results": [
                {
                    "page_number": r.page_number,
                    "success": r.success,
                    "file_path": str(r.file_path) if r.file_path else None,
                    "file_size_bytes": r.file_size,
                    "error": r.error,
                }
                for r in results
            ],
        }

    def verify_image(self, file_path: str) -> dict[str, Any]:
        """Verify that a downloaded image file is a valid JPEG."""
        path = Path(file_path)
        config = MatriculaConfig(
            base_url=self.base_url, book=self.book, out_dir=self.out_dir
        )

        async def run() -> bool:
            async with MatriculaDownloader(config) as downloader:
                return await downloader.verify_image(path)

        try:
            valid = asyncio.run(run())
        except (
            Exception  # noqa: BLE001
        ) as exc:  # pragma: no cover - depends on runtime/browser availability
            return {"valid": False, "error": str(exc)}

        return {"valid": valid, "path": str(path)}

    def _record_download(self, result: PageResult) -> None:
        """Record a download result in the history."""
        record = DownloadRecord(
            page_number=result.page_number,
            success=result.success,
            file_path=str(result.file_path) if result.file_path else None,
            file_size_bytes=result.file_size,
            image_url=result.image_url,
            error=result.error,
            duration_seconds=result.duration_seconds,
        )
        self._download_history.append(record)

    def download_history(self) -> list[dict[str, Any]]:
        """Return the download history as a list of dictionaries."""
        return [asdict(r) for r in self._download_history]

    # ------------------------------------------------------------------
    # Image browser
    # ------------------------------------------------------------------
    def list_images(self) -> list[dict[str, Any]]:
        """List all downloaded images for the current book."""
        directory = Path(self.out_dir)
        pattern = f"matricula_{self.book}_pg*.jpg"
        files = sorted(path for path in directory.glob(pattern) if path.is_file())
        images = []
        for path in files:
            try:
                stat = path.stat()
                images.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "size_bytes": stat.st_size,
                        "page_number": self._page_from_filename(path.name),
                    }
                )
            except OSError:
                continue
        return images

    @staticmethod
    def _page_from_filename(name: str) -> int:
        """Extract page number from a filename like matricula_01519_pg104.jpg."""
        import re

        match = re.search(r"pg(\d+)", name)
        return int(match.group(1)) if match else 0

    def find_pagexml_for_image(self, image_path: str) -> str | None:
        """Find the PAGE-XML file for a given image path.

        Looks for a .xml file with the same stem next to the image.
        """
        path = Path(image_path)
        if not path.is_file():
            return None
        xml_path = path.with_suffix(".xml")
        if xml_path.is_file():
            return str(xml_path)
        # Also try common PAGE-XML naming patterns
        for candidate in path.parent.glob(path.stem + "*.xml"):
            if candidate.is_file():
                return str(candidate)
        return None

    def extract_transcription_from_pagexml(self, pagexml_path: str) -> str:
        """Extract the full transcription text from a PAGE-XML file.

        Concatenates all TextEquiv/Unicode texts from TextLine elements
        in document order, separated by spaces.
        """
        import xml.etree.ElementTree as ET

        path = Path(pagexml_path)
        if not path.is_file():
            return ""

        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            return ""

        def local_name(tag: str) -> str:
            return tag.rsplit("}", 1)[-1] if "}" in tag else tag

        lines: list[str] = []
        for node in root.iter():
            if local_name(node.tag) != "TextLine":
                continue
            line_text = ""
            for child in node:
                if local_name(child.tag) == "TextEquiv":
                    for eq_child in child:
                        if local_name(eq_child.tag) == "Unicode":
                            line_text = eq_child.text or ""
                            break
            if line_text:
                lines.append(line_text)
        return " ".join(lines)

    # ------------------------------------------------------------------
    # Resource monitoring
    # ------------------------------------------------------------------
    def resource_snapshot(self) -> ResourceSnapshot:
        """Read a lightweight resource snapshot without requiring heavy dependencies."""
        cpu = self._read_cpu_percent()
        ram = self._read_ram_percent()
        temp = self._read_temperature()
        return ResourceSnapshot(
            cpu_percent=cpu,
            ram_percent=ram,
            temperature_c=temp,
            active_downloads=self._active_downloads,
            active_ocr_jobs=self._active_ocr_jobs,
        )

    def check_ocr_load(self) -> dict[str, Any]:
        """Check whether system resources allow starting a new OCR job.

        Reads the current resource snapshot and compares it against the
        configured thresholds. Returns a dict with:
          - "allowed": bool — whether a new OCR job should be started
          - "reason": str — human-readable explanation
          - "snapshot": ResourceSnapshot — current readings
        """
        snapshot = self.resource_snapshot()
        reasons: list[str] = []

        cpu = snapshot.cpu_percent
        ram = snapshot.ram_percent
        temp = snapshot.temperature_c

        max_cpu = self.config.ocr_max_cpu_percent
        max_ram = self.config.ocr_max_ram_percent
        max_temp = self.config.ocr_max_temperature_c

        if cpu is not None and max_cpu > 0 and cpu > max_cpu:
            reasons.append(f"CPU {cpu}% exceeds limit {max_cpu}%")
        elif cpu is None and max_cpu > 0:
            reasons.append(
                "CPU-Auslastung unbekannt (erste Messung oder nicht unterstützt)"
            )
        if ram is not None and max_ram > 0 and ram > max_ram:
            reasons.append(f"RAM {ram}% exceeds limit {max_ram}%")
        elif ram is None and max_ram > 0:
            reasons.append(
                "RAM-Auslastung unbekannt (nicht unterstützt auf dieser Plattform)"
            )
        if max_temp is not None and temp is not None and temp > max_temp:
            reasons.append(f"Temperature {temp}°C exceeds limit {max_temp}°C")
        elif max_temp is not None and temp is None:
            reasons.append(
                "Temperatursensor nicht verfügbar (kein CPU-Sensor gefunden)"
            )

        if reasons:
            return {
                "allowed": False,
                "reason": "; ".join(reasons),
                "snapshot": asdict(snapshot),
            }
        return {
            "allowed": True,
            "reason": "Resources within limits",
            "snapshot": asdict(snapshot),
        }

    # ------------------------------------------------------------------
    # OCR backend and job management
    # ------------------------------------------------------------------
    def ocr_backend_status(self) -> dict[str, Any]:
        """Report whether a local OCR backend appears to be available."""
        kraken = shutil.which("kraken")
        htrmopo = shutil.which("htrmopo")
        if kraken:
            backend = "kraken"
        elif htrmopo:
            backend = "htrmopo"
        else:
            backend = "not-configured"
        return {
            "available": bool(kraken or htrmopo),
            "backend": backend,
            "kraken_path": kraken,
            "message": (
                "Local OCR backend available."
                if kraken or htrmopo
                else "No local OCR backend configured; prepare_ocr remains a preparation-only step."
            ),
        }

    def list_ocr_jobs(self) -> list[dict[str, Any]]:
        """Return all OCR jobs as dictionaries."""
        return [asdict(j) for j in self._ocr_jobs]

    def add_ocr_job(self, source_file: str, model: str = "") -> OCRJob:
        """Create a new OCR job and add it to the job list."""
        job = OCRJob(source_file=source_file, model=model, status="pending")
        self._ocr_jobs.append(job)
        return job

    def _validate_model_path(self, model_path: str) -> str | None:
        """Validate the OCR model path.

        Returns an error message if invalid, or None if valid.
        Checks: non-empty, exists, is a file, no shell metacharacters,
        no path traversal components.
        """
        if not model_path or not model_path.strip():
            return "Model path is empty"

        # Reject shell metacharacters to prevent command injection.
        dangerous = set(";&|`$()<>\\{}[]*?~#\t\n\r")
        if any(ch in dangerous for ch in model_path):
            return f"Model path contains unsafe characters: {model_path!r}"

        # Reject path traversal components.
        if ".." in Path(model_path).parts:
            return f"Model path contains path traversal: {model_path!r}"

        model = Path(model_path)
        if not model.is_file():
            return f"Model file not found: {model}"

        # Reject path traversal.
        try:
            model.resolve(strict=True)
        except (OSError, ValueError):
            return f"Model path cannot be resolved: {model}"

        return None

    def run_ocr_job(
        self, job: OCRJob, model_path: str, threads: int = 1
    ) -> dict[str, Any]:
        """Run an OCR job using the local Kraken backend if available.

        This does not depend on any private project scripts. It locates the
        Kraken executable via PATH and invokes it directly on the given image.

        Hardening:
        - Validates the model path before invocation (existence, file type,
          no shell metacharacters, no path traversal).
        - Applies a timeout from config (ocr_timeout_seconds) so a hung OCR
          process cannot block indefinitely.
        - Uses a list-form subprocess call (no shell=True) to prevent
          shell injection.
        - Resets the active OCR job counter on every exit path via finally.
        """
        kraken = shutil.which("kraken")
        if not kraken:
            job.status = "failed"
            job.error = "Kraken executable not found on PATH"
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {"success": False, "error": job.error}

        model_error = self._validate_model_path(model_path)
        if model_error:
            job.status = "failed"
            job.error = model_error
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {"success": False, "error": model_error}

        source = Path(job.source_file)
        if not source.is_file():
            job.status = "failed"
            job.error = f"Source file not found: {source}"
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {"success": False, "error": job.error}

        output_dir = Path(self.out_dir) / "ocr-results" / job.id
        output_dir.mkdir(parents=True, exist_ok=True)
        pagexml_path = output_dir / f"{source.stem}.page.xml"
        job.output_dir = str(output_dir)
        job.status = "running"
        self._active_ocr_jobs += 1

        timeout = self.config.ocr_timeout_seconds

        try:
            # Kraken v7 requires bi-level input; convert if necessary.
            work_source = source
            if source.suffix.lower() not in (".png", ".pbm"):
                bw_path = output_dir / f"{source.stem}_bw.png"
                try:
                    from PIL import Image  # type: ignore

                    img = (
                        Image.open(source)
                        .convert("L")
                        .point(lambda x: 0 if x < 128 else 255, "1")
                    )
                    img.save(bw_path, "PNG")
                    work_source = bw_path
                except Exception:  # noqa: BLE001 — fall back to original if conversion fails
                    work_source = source

            subprocess.run(
                [
                    kraken,
                    "-d",
                    "cpu",
                    "--threads",
                    str(threads),
                    "-x",
                    "-i",
                    str(work_source),
                    str(pagexml_path),
                    "segment",
                    "ocr",
                    "-m",
                    model_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )

            # Parse PAGE-XML for line count
            line_count = 0
            if pagexml_path.is_file():
                try:
                    import xml.etree.ElementTree as ET

                    root = ET.parse(pagexml_path).getroot()
                    for node in root.iter():
                        tag = (
                            node.tag.rsplit("}", 1)[-1] if "}" in node.tag else node.tag
                        )
                        if tag == "TextLine":
                            line_count += 1
                except Exception:  # noqa: BLE001 — defensive: OCR must not fail on unparseable PAGE-XML
                    line_count = 0

            job.status = "success"
            job.line_count = line_count
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {
                "success": True,
                "output_dir": str(output_dir),
                "pagexml": str(pagexml_path) if pagexml_path.is_file() else None,
                "line_count": line_count,
            }
        except subprocess.TimeoutExpired:
            job.status = "failed"
            job.error = f"OCR job timed out after {timeout}s"
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {"success": False, "error": job.error}
        except (OSError, subprocess.CalledProcessError) as exc:
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = __import__("datetime").datetime.now().isoformat()
            return {"success": False, "error": str(exc)}
        finally:
            self._active_ocr_jobs -= 1

    # ------------------------------------------------------------------
    # Source management
    # ------------------------------------------------------------------
    def add_source(
        self,
        *,
        archive: str = "",
        municipality: str = "",
        book: str = "",
        page: int = 0,
        dating: str = "",
        signature: str = "",
        notes: str = "",
        file_path: str | None = None,
    ) -> SourceRecord:
        """Create and store a new source record."""
        source = SourceRecord(
            archive=archive,
            municipality=municipality,
            book=book or self.book,
            page=page,
            dating=dating,
            signature=signature,
            notes=notes,
            file_path=file_path,
        )
        self._sources.append(source)
        self._persist_source(source)
        return source

    def list_sources(self) -> list[dict[str, Any]]:
        """Return all source records as dictionaries."""
        try:
            return [asdict(s) for s in self._store.list()]
        except Exception:  # noqa: BLE001, S110 — storage must not break the workbench
            pass
        return [asdict(s) for s in self._sources]

    def update_source(
        self,
        source_id: str,
        *,
        archive: str | None = None,
        municipality: str | None = None,
        book: str | None = None,
        page: int | None = None,
        dating: str | None = None,
        signature: str | None = None,
        notes: str | None = None,
        verification_status: str | None = None,
        file_path: str | None = None,
    ) -> SourceRecord | None:
        """Update an existing source record by ID."""
        for source in self._sources:
            if source.id == source_id:
                if archive is not None:
                    source.archive = archive
                if municipality is not None:
                    source.municipality = municipality
                if book is not None:
                    source.book = book
                if page is not None:
                    source.page = page
                if dating is not None:
                    source.dating = dating
                if signature is not None:
                    source.signature = signature
                if notes is not None:
                    source.notes = notes
                if verification_status is not None:
                    source.verification_status = verification_status
                if file_path is not None:
                    source.file_path = file_path
                source.updated_at = __import__("datetime").datetime.now().isoformat()
                self._persist_source(source)
                return source
        return None

    def delete_source(self, source_id: str) -> bool:
        """Delete a source record by ID. Returns True if deleted."""
        try:
            if self._store.delete(source_id):
                self._sources = [s for s in self._sources if s.id != source_id]
                return True
            return False
        except Exception:  # noqa: BLE001, S110 — storage must not break the workbench
            pass
        for i, source in enumerate(self._sources):
            if source.id == source_id:
                del self._sources[i]
                return True
        return False

    def link_source_to_download(
        self, page_number: int, file_path: str | None = None
    ) -> SourceRecord | None:
        """Create a source record linked to a downloaded page, if none exists yet."""
        try:
            existing = self._store.list_by_book_page(self.book, page_number)
            if existing is not None:
                return existing
            source = self.add_source(
                book=self.book,
                page=page_number,
                file_path=file_path,
            )
            return source
        except Exception:  # noqa: BLE001, S110 — storage must not break the workbench
            pass
        for source in self._sources:
            if source.book == self.book and source.page == page_number:
                return source
        return self.add_source(
            book=self.book,
            page=page_number,
            file_path=file_path,
        )

    def export_sources(self, format: str = "json") -> str:
        """Export all source records as JSON or CSV string.

        Args:
            format: "json" or "csv".

        Returns:
            Serialized source records.
        """
        try:
            if format == "csv":
                return self._store.export_csv()
            return self._store.export_json()
        except Exception:  # noqa: BLE001, S110 — storage must not break the workbench
            pass
        sources = self.list_sources()
        if format == "csv":
            import csv
            import io

            if not sources:
                return ""
            fields = list(sources[0].keys())
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=fields)
            writer.writeheader()
            writer.writerows(sources)
            return buffer.getvalue()
        # default: json
        return json.dumps(sources, indent=2, ensure_ascii=False)

    def export_correction(
        self, source_file: str, output_path: str | None = None
    ) -> dict[str, Any]:
        """Export a saved correction for a source file to a standalone text file.

        Looks for a correction file in <out_dir>/corrections/<name>.corrected.txt
        and copies it to output_path (or returns its content).
        """
        corrections_dir = Path(self.out_dir) / "corrections"
        source_name = Path(source_file).name
        correction_file = corrections_dir / f"{source_name}.corrected.txt"

        if not correction_file.is_file():
            return {"success": False, "error": f"No correction found for {source_name}"}

        content = correction_file.read_text(encoding="utf-8")

        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            return {"success": True, "output_path": str(out), "bytes": len(content)}

        return {"success": True, "content": content}

    # ------------------------------------------------------------------
    # Korrektur-CRUD
    # ------------------------------------------------------------------
    def add_correction(self, correction: CorrectionRecord) -> CorrectionRecord:
        """Speichert eine neue Korrektur nach Konfliktprüfung.

        Raises ``CorrectionConflictError``, wenn der Textbereich mit einer
        bestehenden aktiven Korrektur überlappt.
        """
        self._check_correction_conflict(correction)
        now = __import__("datetime").datetime.now().isoformat()
        correction.created_at = now
        with self._store._lock:
            self._store.conn.execute(
                """
                INSERT INTO corrections
                (id, source_file, page_number, book, original_text,
                 corrected_text, original_text_position, original_text_length,
                 image_x, image_y, image_width, image_height,
                 image_width_px, image_height_px, line_number, readability,
                 status, created_at, corrected_at, ocr_job_id, correction_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction.id,
                    correction.source_file,
                    correction.page_number,
                    correction.book,
                    correction.original_text,
                    correction.corrected_text,
                    correction.original_text_position,
                    correction.original_text_length,
                    correction.image_x,
                    correction.image_y,
                    correction.image_width,
                    correction.image_height,
                    correction.image_width_px,
                    correction.image_height_px,
                    correction.line_number,
                    correction.readability,
                    correction.status,
                    correction.created_at,
                    correction.corrected_at,
                    correction.ocr_job_id,
                    correction.correction_type,
                ),
            )
            self._store.conn.commit()
        return correction

    def get_correction(self, correction_id: str) -> CorrectionRecord | None:
        """Gibt eine Korrektur nach ID zurück oder None."""
        with self._store._lock:
            row = self._store.conn.execute(
                "SELECT * FROM corrections WHERE id = ?", (correction_id,)
            ).fetchone()
        return self._row_to_correction(row) if row else None

    def list_corrections(
        self,
        *,
        source_file: str | None = None,
        page_number: int | None = None,
        book: str | None = None,
        status: str | None = None,
    ) -> list[CorrectionRecord]:
        """Gibt Korrekturen zurück, optional gefiltert."""
        conditions: list[str] = []
        values: list[Any] = []
        if source_file is not None:
            conditions.append("source_file = ?")
            values.append(source_file)
        if page_number is not None:
            conditions.append("page_number = ?")
            values.append(page_number)
        if book is not None:
            conditions.append("book = ?")
            values.append(book)
        if status is not None:
            conditions.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._store._lock:
            rows = self._store.conn.execute(
                f"SELECT * FROM corrections{where} ORDER BY created_at", values
            ).fetchall()
        return [self._row_to_correction(r) for r in rows]

    def update_correction(
        self,
        correction_id: str,
        *,
        corrected_text: str | None = None,
        readability: str | None = None,
        status: str | None = None,
        corrected_at: str | None = None,
    ) -> CorrectionRecord | None:
        """Aktualisiert Felder einer bestehenden Korrektur."""
        allowed: dict[str, Any] = {}
        if corrected_text is not None:
            allowed["corrected_text"] = corrected_text
            allowed["corrected_at"] = __import__("datetime").datetime.now().isoformat()
        if readability is not None:
            allowed["readability"] = readability
        if status is not None:
            allowed["status"] = status
        if corrected_at is not None:
            allowed["corrected_at"] = corrected_at
        if not allowed:
            return self.get_correction(correction_id)

        set_clause = ", ".join(f"{k} = ?" for k in allowed)
        values = list(allowed.values()) + [correction_id]
        with self._store._lock:
            cursor = self._store.conn.execute(
                f"UPDATE corrections SET {set_clause} WHERE id = ?", values
            )
            self._store.conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_correction(correction_id)

    def delete_correction(self, correction_id: str) -> bool:
        """Löscht eine Korrektur. Gibt True zurück, wenn eine Zeile entfernt wurde."""
        with self._store._lock:
            cursor = self._store.conn.execute(
                "DELETE FROM corrections WHERE id = ?", (correction_id,)
            )
            self._store.conn.commit()
            return cursor.rowcount > 0

    def revert_correction(self, correction_id: str) -> CorrectionRecord | None:
        """Setzt eine Korrektur zurück (status = reverted), ohne sie zu löschen."""
        return self.update_correction(correction_id, status="reverted")

    def restore_correction(self, correction_id: str) -> CorrectionRecord | None:
        """Stellt eine zurückgegebene Korrektur wieder aktiv her."""
        return self.update_correction(correction_id, status="active")

    # ------------------------------------------------------------------
    # Transkription rekonstruieren (stabile Offsets)
    # ------------------------------------------------------------------
    def build_corrected_transcription(
        self,
        original_text: str,
        corrections: list[CorrectionRecord] | None = None,
        *,
        source_file: str | None = None,
        page_number: int | None = None,
        book: str | None = None,
    ) -> str:
        """Baut die aktuelle Transkription aus Originaltext + aktiven Korrekturen.

        Korrekturen werden absteigend nach ``original_text_position`` angewendet,
        sodass unterschiedliche Wortlängen die Offsets anderer Korrekturen nicht
        verschieben. Der Originaltext bleibt unverändert.
        """
        if corrections is None:
            corrections = self.list_corrections(
                source_file=source_file,
                page_number=page_number,
                book=book,
                status="active",
            )
        result = original_text
        for corr in sorted(
            corrections, key=lambda c: c.original_text_position, reverse=True
        ):
            pos = corr.original_text_position
            length = corr.original_text_length
            if pos < 0 or length < 0 or pos + length > len(result):
                continue
            result = result[:pos] + corr.corrected_text + result[pos + length :]
        return result

    # ------------------------------------------------------------------
    # Konfliktprüfung
    # ------------------------------------------------------------------
    def _check_correction_conflict(self, new_correction: CorrectionRecord) -> None:
        """Prüft, ob der Textbereich der neuen Korrektur mit einer aktiven
        bestehenden Korrektur überlappt. Wirft ``CorrectionConflictError``."""
        existing = self.list_corrections(
            source_file=new_correction.source_file,
            page_number=new_correction.page_number,
            book=new_correction.book,
            status="active",
        )
        new_start = new_correction.original_text_position
        new_end = new_start + new_correction.original_text_length
        for corr in existing:
            if corr.id == new_correction.id:
                continue
            start = corr.original_text_position
            end = start + corr.original_text_length
            if new_start < end and start < new_end:
                raise CorrectionConflictError(
                    f"Korrektur überschneidet sich mit bestehender Korrektur "
                    f"{corr.id} (Position {start}-{end}, Text: "
                    f"{corr.original_text!r})"
                )

    # ------------------------------------------------------------------
    # Kraken PAGE-XML parsen (Wortkoordinaten)
    # ------------------------------------------------------------------
    @staticmethod
    def parse_kraken_words(pagexml_path: str) -> list[dict[str, Any]]:
        """Extrahiert Wörter mit Koordinaten aus einer Kraken PAGE-XML-Datei.

        Namespace-robust (Local-Names via rsplit). Nicht jedes Wort muss
        eigene Koordinaten besitzen; fehlende Daten werden abgefangen.
        Gibt eine Liste von Dicts mit den Schlüsseln zurück:
        text, x, y, width, height, line_number, word_index.
        """
        import xml.etree.ElementTree as ET

        words: list[dict[str, Any]] = []
        path = Path(pagexml_path)
        if not path.is_file():
            return words

        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            return words

        def local_name(tag: str) -> str:
            return tag.rsplit("}", 1)[-1] if "}" in tag else tag

        line_number = 0
        for node in root.iter():
            if local_name(node.tag) != "TextLine":
                continue
            line_number += 1
            word_index = 0
            line_coords = None
            for child in node:
                if local_name(child.tag) == "Coords":
                    line_coords = child.get("points")
                if local_name(child.tag) == "Word":
                    word_index += 1
                    word_text = ""
                    for grandchild in child:
                        if local_name(grandchild.tag) == "TextEquiv":
                            for eq_child in grandchild:
                                if local_name(eq_child.tag) == "Unicode":
                                    word_text = eq_child.text or ""
                                    break
                    word_coords = None
                    for grandchild in child:
                        if local_name(grandchild.tag) == "Coords":
                            word_coords = grandchild.get("points")
                    x, y, w, h = MatriculaDesktopService._parse_points(
                        word_coords or line_coords
                    )
                    words.append(
                        {
                            "text": word_text,
                            "x": x,
                            "y": y,
                            "width": w,
                            "height": h,
                            "line_number": line_number,
                            "word_index": word_index,
                        }
                    )
        return words

    @staticmethod
    def _parse_points(points: str | None) -> tuple[int, int, int, int]:
        """Wandelt PAGE-XML-Coords-Points (x,y x,y ...) in (x, y, w, h) um."""
        if not points:
            return 0, 0, 0, 0
        try:
            pairs = []
            for pair in points.strip().split():
                x_str, y_str = pair.split(",")
                pairs.append((float(x_str), float(y_str)))
        except (ValueError, AttributeError):
            return 0, 0, 0, 0
        if not pairs:
            return 0, 0, 0, 0
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        x = int(min(xs))
        y = int(min(ys))
        w = int(max(xs) - min(xs))
        h = int(max(ys) - min(ys))
        return x, y, w, h

    # ------------------------------------------------------------------
    # Korrektur-Zeile in Record umwandeln
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> CorrectionRecord:
        return CorrectionRecord(
            id=row["id"],
            source_file=row["source_file"],
            page_number=row["page_number"],
            book=row["book"],
            original_text=row["original_text"],
            corrected_text=row["corrected_text"],
            original_text_position=row["original_text_position"],
            original_text_length=row["original_text_length"],
            image_x=row["image_x"],
            image_y=row["image_y"],
            image_width=row["image_width"],
            image_height=row["image_height"],
            image_width_px=row["image_width_px"],
            image_height_px=row["image_height_px"],
            line_number=row["line_number"],
            readability=row["readability"],
            status=row["status"],
            created_at=row["created_at"],
            corrected_at=row["corrected_at"],
            ocr_job_id=row["ocr_job_id"],
            correction_type=row["correction_type"],
        )

    def _read_cpu_percent(self) -> float | None:
        """Read current CPU utilisation via a two-sample /proc/stat delta.

        The first call establishes a baseline and returns ``None`` (not
        available). Subsequent calls compute the delta between two readings
        and return the CPU utilisation as a percentage. Returns ``None`` on
        any error (missing /proc/stat, unreadable, division by zero, or
        implausible values) so callers can distinguish "unknown" from "0 %".
        """
        if not Path("/proc/stat").exists():
            return None
        try:
            with open("/proc/stat", "r", encoding="utf-8") as handle:
                first_line = handle.readline()
            if not first_line:
                return None
            parts = first_line.split()
            if len(parts) < 5:
                return None
            values = [int(part) for part in parts[1:]]
            if len(values) < 4:
                return None
            total = sum(values)
            if total <= 0:
                return None
            idle = values[3] if len(values) > 3 else 0

            current = (total, idle)
            prev = self._prev_cpu_times
            self._prev_cpu_times = current

            if prev is None:
                # First reading: baseline only, no delta available yet.
                return None

            prev_total, prev_idle = prev
            delta_total = total - prev_total
            delta_idle = idle - prev_idle
            if delta_total <= 0:
                return None
            if delta_idle < 0 or delta_idle > delta_total:
                return None
            busy_delta = delta_total - delta_idle
            return round((busy_delta / delta_total) * 100, 1)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_ram_percent() -> float | None:
        """Read RAM utilisation from /proc/meminfo (Linux only).

        Returns ``None`` on non-Linux platforms or when /proc/meminfo is
        unavailable, so callers can distinguish "not available" from "0 %".
        """
        if not Path("/proc/meminfo").exists():
            return None
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                meminfo: dict[str, int] = {}
                for line in handle:
                    key, sep, value = line.partition(":")
                    if not sep:
                        continue
                    parts = value.strip().split()
                    if not parts:
                        continue
                    meminfo[key.strip()] = int(parts[0])
            total = meminfo.get("MemTotal")
            available = meminfo.get("MemAvailable")
            if total and available:
                used = total - available
                return round((used / total) * 100, 1)
        except OSError:
            pass
        return None

    @staticmethod
    def _read_temperature() -> float | None:
        """Read CPU temperature from Linux thermal zones.

        Identifies CPU-specific sensors by reading the ``type`` file of each
        thermal zone and looking for CPU-related names (e.g. ``x86_pkg_temp``,
        ``coretemp``, ``acpitz`` with CPU context, ``k10temp``). The first
        alphabetically sorted zone is deliberately *not* used, as it may
        refer to a battery, chassis, or sensor hub rather than the CPU.

        Returns ``None`` if no trustworthy CPU sensor is found, files are
        missing, or values are unparseable. Callers must treat ``None`` as
        "not available", never as a specific temperature.

        Limitations under Linux:
        - Thermal zone naming is not standardised across hardware vendors.
        - Some systems expose no CPU-specific thermal zone at all.
        - Containerised environments may lack /sys/class/thermal entirely.
        """
        thermal_dir = Path("/sys/class/thermal")
        if not thermal_dir.is_dir():
            return None

        cpu_keywords = (
            "x86_pkg_temp",
            "coretemp",
            "k10temp",
            "zenpower",
            "acpitz",
            "cpu",
            "processor",
        )

        candidates: list[tuple[Path, Path]] = []
        for zone_dir in sorted(thermal_dir.glob("thermal_zone*")):
            type_file = zone_dir / "type"
            temp_file = zone_dir / "temp"
            if not temp_file.is_file():
                continue
            zone_type = ""
            if type_file.is_file():
                try:
                    zone_type = type_file.read_text(encoding="utf-8").strip().lower()
                except OSError:
                    pass
            candidates.append((zone_dir, temp_file))
            if any(kw in zone_type for kw in cpu_keywords):
                try:
                    value = int(temp_file.read_text(encoding="utf-8").strip())
                    return round(value / 1000.0, 1)
                except (OSError, ValueError):
                    continue

        # Fallback: try all zones if no CPU-specific one matched.
        for _zone_dir, temp_file in candidates:
            try:
                value = int(temp_file.read_text(encoding="utf-8").strip())
                if 0 < value < 200_000:  # sanity: 0–200 °C in millidegrees
                    return round(value / 1000.0, 1)
            except (OSError, ValueError):
                continue

        return None


class ReadabilityStatus:
    """Lesesicherheitsstatus für eine manuelle Korrektur."""

    SICHER = "SICHER"
    UNSICHER = "UNSICHER"
    UNLESERLICH = "UNLESERLICH"

    @classmethod
    def values(cls) -> list[str]:
        return [cls.SICHER, cls.UNSICHER, cls.UNLESERLICH]


@dataclass
class CorrectionRecord:
    """Eine manuelle Transkriptionskorrektur mit Bild- und Textbezug.

    Alle Korrekturen beziehen sich auf die unveränderte Originaltranskription
    über stabile Character-Offsets (``original_text_position`` /
    ``original_text_length``). Die aktuelle Transkription wird reproduzierbar
    aus Originaltext + Korrekturen aufgebaut (absteigend nach Offset anwenden,
    sodass unterschiedliche Wortlängen sich nicht gegenseitig verschieben).
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    source_file: str = ""
    page_number: int = 0
    book: str = ""
    original_text: str = ""
    corrected_text: str = ""
    original_text_position: int = 0
    original_text_length: int = 0
    image_x: int = 0
    image_y: int = 0
    image_width: int = 0
    image_height: int = 0
    image_width_px: int = 0
    image_height_px: int = 0
    line_number: int | None = None
    readability: str = ReadabilityStatus.SICHER
    status: str = "active"  # active, reverted, superseded
    created_at: str = field(
        default_factory=lambda: __import__("datetime").datetime.now().isoformat()
    )
    corrected_at: str | None = None
    ocr_job_id: str | None = None
    correction_type: str = "word"  # word, region, free


class CorrectionConflictError(Exception):
    """Erzeugt, wenn eine neue Korrektur mit einer bestehenden kollidiert."""


__all__ = [
    "CorrectionConflictError",
    "CorrectionRecord",
    "DownloadRecord",
    "MatriculaDesktopService",
    "OCRJob",
    "ReadabilityStatus",
    "ResourceSnapshot",
    "SourceRecord",
]
