"""Provider configuration for external genealogical archives.

This module loads and validates the machine-readable provider configuration
from ``providers.json``.  Provider data is kept strictly separate from the
SSRF security logic in :mod:`matricula_toolkit.security` and
:mod:`matricula_toolkit.local_proxy`.

Providers are disabled by default.  Unknown hosts, unconfirmed redirect
targets and unknown viewer properties are never automatically enabled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matricula_toolkit.exceptions import ConfigurationError

# Required top-level fields for every provider entry.
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "homepage",
        "regions",
        "religions",
        "record_types",
        "provider_type",
        "viewer_type",
        "entry_hosts",
        "content_hosts",
        "redirect_hosts",
        "authentication",
        "image_access",
        "terms_url",
        "terms_checked_at",
        "verification_status",
        "enabled",
        "notes",
    }
)

# Allowed values for closed vocabularies.
_VALID_VIEWER_TYPES: frozenset[str] = frozenset(
    {"unknown", "html_viewer", "tile_viewer", "iiif", "direct_image", "index_only"}
)

_VALID_AUTHENTICATION: frozenset[str] = frozenset(
    {
        "public",
        "account_required",
        "paid_subscription",
        "collection_restricted",
        "unknown",
    }
)

_VALID_IMAGE_ACCESS: frozenset[str] = frozenset(
    {"manual_only", "permitted_after_review", "written_permission_required", "unknown"}
)

_VALID_VERIFICATION_STATUS: frozenset[str] = frozenset(
    {"unverified", "partially_verified", "verified"}
)

_VALID_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"scan_archive", "search_index", "catalog"}
)


class ProviderError(ConfigurationError):
    """Raised when provider configuration is invalid or a provider is unavailable."""


@dataclass(frozen=True)
class Provider:
    """A single configured genealogical archive provider.

    All fields are read-only after construction.  Unknown or unverified
    values are represented as the string ``"unknown"``.
    """

    id: str
    name: str
    homepage: str
    regions: list[str]
    religions: list[str]
    record_types: list[str]
    provider_type: str
    viewer_type: str
    entry_hosts: list[str]
    content_hosts: list[str]
    redirect_hosts: list[str]
    authentication: str
    image_access: str
    terms_url: str
    terms_checked_at: str
    verification_status: str
    enabled: bool
    notes: str

    @property
    def all_hosts(self) -> list[str]:
        """Return every host declared by this provider (entry + content + redirect)."""
        return (
            list(self.entry_hosts)
            + list(self.content_hosts)
            + list(self.redirect_hosts)
        )

    def is_entry_host(self, host: str) -> bool:
        """Return True if *host* is an exact entry host of this provider."""
        return host in self.entry_hosts

    def is_content_host(self, host: str) -> bool:
        """Return True if *host* is an exact content host of this provider."""
        return host in self.content_hosts

    def is_redirect_host(self, host: str) -> bool:
        """Return True if *host* is an exact redirect host of this provider."""
        return host in self.redirect_hosts


def _load_raw(path: Path) -> dict[str, Any]:
    """Load and parse the provider JSON file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ProviderError(f"Provider configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Invalid provider configuration JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("Provider configuration must be a JSON object")
    return data


def _validate_provider_entry(raw: dict[str, Any], index: int) -> None:
    """Validate a single provider entry from the raw JSON data."""
    if not isinstance(raw, dict):
        raise ProviderError(f"Provider entry at index {index} is not an object")

    missing = _REQUIRED_FIELDS - set(raw.keys())
    if missing:
        raise ProviderError(
            f"Provider entry at index {index} missing required fields: "
            + ", ".join(sorted(missing))
        )

    if not isinstance(raw["id"], str) or not raw["id"].strip():
        raise ProviderError(f"Provider entry at index {index} has invalid 'id'")
    if not isinstance(raw["name"], str) or not raw["name"].strip():
        raise ProviderError(f"Provider entry at index {index} has invalid 'name'")
    if not isinstance(raw["homepage"], str) or not raw["homepage"].strip():
        raise ProviderError(f"Provider entry at index {index} has invalid 'homepage'")

    for field_name in (
        "regions",
        "religions",
        "record_types",
        "entry_hosts",
        "content_hosts",
        "redirect_hosts",
    ):
        value = raw[field_name]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ProviderError(
                f"Provider entry at index {index} has invalid '{field_name}' (must be list of strings)"
            )

    if raw["provider_type"] not in _VALID_PROVIDER_TYPES:
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'provider_type': {raw['provider_type']!r}"
        )
    if raw["viewer_type"] not in _VALID_VIEWER_TYPES:
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'viewer_type': {raw['viewer_type']!r}"
        )
    if raw["authentication"] not in _VALID_AUTHENTICATION:
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'authentication': {raw['authentication']!r}"
        )
    if raw["image_access"] not in _VALID_IMAGE_ACCESS:
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'image_access': {raw['image_access']!r}"
        )
    if raw["verification_status"] not in _VALID_VERIFICATION_STATUS:
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'verification_status': {raw['verification_status']!r}"
        )
    if not isinstance(raw["enabled"], bool):
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'enabled' (must be boolean)"
        )
    if (
        not isinstance(raw["terms_checked_at"], str)
        or not raw["terms_checked_at"].strip()
    ):
        raise ProviderError(
            f"Provider entry at index {index} has invalid 'terms_checked_at'"
        )
    if not isinstance(raw["notes"], str):
        raise ProviderError(f"Provider entry at index {index} has invalid 'notes'")


def load_providers(path: Path | None = None) -> dict[str, Provider]:
    """Load and validate all providers from the configuration file.

    Args:
        path: Optional path to the providers JSON file.  Defaults to
            ``providers.json`` next to this module.

    Returns:
        A dictionary mapping provider ID to :class:`Provider`.

    Raises:
        ProviderError: If the file cannot be read, is invalid JSON, or
            contains duplicate IDs or invalid entries.
    """
    if path is None:
        path = Path(__file__).with_name("providers.json")

    data = _load_raw(path)
    version = data.get("version")
    if version != 1:
        raise ProviderError(f"Unsupported provider configuration version: {version!r}")

    raw_providers = data.get("providers")
    if not isinstance(raw_providers, list):
        raise ProviderError("'providers' must be a list")

    providers: dict[str, Provider] = {}
    for index, raw in enumerate(raw_providers):
        _validate_provider_entry(raw, index)
        provider_id = raw["id"]
        if provider_id in providers:
            raise ProviderError(f"Duplicate provider ID: {provider_id!r}")
        providers[provider_id] = Provider(
            id=raw["id"],
            name=raw["name"],
            homepage=raw["homepage"],
            regions=list(raw["regions"]),
            religions=list(raw["religions"]),
            record_types=list(raw["record_types"]),
            provider_type=raw["provider_type"],
            viewer_type=raw["viewer_type"],
            entry_hosts=list(raw["entry_hosts"]),
            content_hosts=list(raw["content_hosts"]),
            redirect_hosts=list(raw["redirect_hosts"]),
            authentication=raw["authentication"],
            image_access=raw["image_access"],
            terms_url=raw["terms_url"],
            terms_checked_at=raw["terms_checked_at"],
            verification_status=raw["verification_status"],
            enabled=raw["enabled"],
            notes=raw["notes"],
        )

    return providers


def get_provider(providers: dict[str, Provider], provider_id: str) -> Provider | None:
    """Return the provider with *provider_id*, or None if not found."""
    return providers.get(provider_id)


def get_enabled_providers(providers: dict[str, Provider]) -> list[Provider]:
    """Return all enabled providers, sorted by ID."""
    return sorted(
        (p for p in providers.values() if p.enabled),
        key=lambda p: p.id,
    )


def get_disabled_providers(providers: dict[str, Provider]) -> list[Provider]:
    """Return all disabled providers, sorted by ID."""
    return sorted(
        (p for p in providers.values() if not p.enabled),
        key=lambda p: p.id,
    )
