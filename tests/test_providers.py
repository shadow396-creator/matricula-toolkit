"""Tests for the provider configuration module (matricula_toolkit.providers)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matricula_toolkit.exceptions import ConfigurationError
from matricula_toolkit.providers import (
    ProviderError,
    get_disabled_providers,
    get_enabled_providers,
    get_provider,
    load_providers,
)

# ------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------


def test_load_providers_returns_all_ten():
    """Loading the bundled configuration returns all 10 providers."""
    providers = load_providers()
    assert len(providers) == 10


def test_load_providers_known_ids():
    """Expected provider IDs are present."""
    providers = load_providers()
    expected = {
        "matricula-online",
        "digitalarkivet",
        "raduraksti",
        "jewishgen",
        "archion",
        "familysearch",
        "antenati",
        "genbaza",
        "arkivalieronline",
        "slave-societies-digital-archive",
    }
    assert set(providers.keys()) == expected


def test_load_providers_matricula_online_fields():
    """Matricula Online provider has verified fields."""
    providers = load_providers()
    p = providers["matricula-online"]
    assert p.id == "matricula-online"
    assert p.name == "Matricula Online"
    assert p.enabled is True
    assert p.verification_status == "verified"
    assert p.viewer_type == "html_viewer"
    assert p.authentication == "public"
    assert p.image_access == "manual_only"
    assert "data.matricula-online.eu" in p.entry_hosts
    assert "img.data.matricula-online.eu" in p.content_hosts


def test_load_providers_disabled_providers():
    """Unverified providers are disabled by default."""
    providers = load_providers()
    for pid in (
        "antenati",
        "genbaza",
        "arkivalieronline",
        "slave-societies-digital-archive",
    ):
        p = providers[pid]
        assert p.enabled is False
        assert p.verification_status == "unverified"


def test_get_enabled_and_disabled():
    """get_enabled_providers and get_disabled_providers split correctly."""
    providers = load_providers()
    enabled = get_enabled_providers(providers)
    disabled = get_disabled_providers(providers)
    enabled_ids = {p.id for p in enabled}
    disabled_ids = {p.id for p in disabled}
    assert enabled_ids == {
        "matricula-online",
        "digitalarkivet",
        "raduraksti",
        "jewishgen",
        "archion",
        "familysearch",
    }
    assert disabled_ids == {
        "antenati",
        "genbaza",
        "arkivalieronline",
        "slave-societies-digital-archive",
    }
    assert len(enabled) + len(disabled) == len(providers)


def test_get_provider_unknown_returns_none():
    """get_provider returns None for an unknown ID."""
    providers = load_providers()
    assert get_provider(providers, "nonexistent-provider") is None


def test_provider_all_hosts_property():
    """all_hosts is a property combining entry, content and redirect hosts."""
    providers = load_providers()
    p = providers["matricula-online"]
    hosts = p.all_hosts  # property, not a method
    assert "data.matricula-online.eu" in hosts
    assert "img.data.matricula-online.eu" in hosts


def test_provider_is_entry_host():
    providers = load_providers()
    p = providers["matricula-online"]
    assert p.is_entry_host("data.matricula-online.eu") is True
    assert p.is_entry_host("evil.example.com") is False


def test_provider_is_content_host():
    providers = load_providers()
    p = providers["matricula-online"]
    assert p.is_content_host("img.data.matricula-online.eu") is True
    assert p.is_content_host("evil.example.com") is False


def test_provider_is_redirect_host_empty():
    providers = load_providers()
    p = providers["matricula-online"]
    assert p.is_redirect_host("anything.example.com") is False


def test_provider_is_frozen():
    """Provider instances are immutable (frozen dataclass)."""
    providers = load_providers()
    p = providers["matricula-online"]
    with pytest.raises(AttributeError):
        p.enabled = True  # type: ignore[misc]


def test_provider_error_is_configuration_error():
    """ProviderError is a ConfigurationError."""
    assert issubclass(ProviderError, ConfigurationError)


# ------------------------------------------------------------------
# Validation: missing fields
# ------------------------------------------------------------------


def _write_tmp_providers(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "providers.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _base_provider_entry() -> dict:
    return {
        "id": "test-provider",
        "name": "Test",
        "homepage": "https://example.com/",
        "regions": [],
        "religions": [],
        "record_types": [],
        "provider_type": "scan_archive",
        "viewer_type": "unknown",
        "entry_hosts": [],
        "content_hosts": [],
        "redirect_hosts": [],
        "authentication": "public",
        "image_access": "unknown",
        "terms_url": "https://example.com/terms",
        "terms_checked_at": "2026-10-29",
        "verification_status": "unverified",
        "enabled": False,
        "notes": "",
    }


def test_load_providers_missing_required_field(tmp_path):
    """A provider entry missing a required field is rejected."""
    data = {
        "version": 1,
        "providers": [
            {
                "id": "test-provider",
                "name": "Test",
                # missing homepage and many other fields
            },
        ],
    }
    path = _write_tmp_providers(tmp_path, data)
    with pytest.raises(ProviderError, match="missing required fields"):
        load_providers(path)


def test_load_providers_duplicate_id(tmp_path):
    """Duplicate provider IDs are rejected."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry, dict(entry, name="Second")]}
    path = _write_tmp_providers(tmp_path, data)
    with pytest.raises(ProviderError, match="Duplicate provider ID"):
        load_providers(path)


def test_load_providers_invalid_viewer_type(tmp_path):
    """An invalid viewer_type is rejected."""
    entry = _base_provider_entry()
    entry["viewer_type"] = "not_a_real_viewer"
    data = {"version": 1, "providers": [entry]}
    path = _write_tmp_providers(tmp_path, data)
    with pytest.raises(ProviderError, match="invalid 'viewer_type'"):
        load_providers(path)


def test_load_providers_invalid_authentication(tmp_path):
    """An invalid authentication value is rejected."""
    entry = _base_provider_entry()
    entry["authentication"] = "not_a_real_auth"
    data = {"version": 1, "providers": [entry]}
    path = _write_tmp_providers(tmp_path, data)
    with pytest.raises(ProviderError, match="invalid 'authentication'"):
        load_providers(path)


def test_load_providers_file_not_found():
    """A missing configuration file raises ProviderError."""
    with pytest.raises(ProviderError, match="not found"):
        load_providers(Path("/nonexistent/path/providers.json"))


def test_load_providers_invalid_json(tmp_path):
    """Invalid JSON is rejected."""
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ProviderError, match="Invalid provider configuration JSON"):
        load_providers(path)


def test_load_providers_unsupported_version(tmp_path):
    """An unsupported version is rejected."""
    data = {"version": 99, "providers": []}
    path = _write_tmp_providers(tmp_path, data)
    with pytest.raises(
        ProviderError, match="Unsupported provider configuration version"
    ):
        load_providers(path)
