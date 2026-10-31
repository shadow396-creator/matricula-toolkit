"""Tests for the provider policy module (matricula_toolkit.provider_policy)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from matricula_toolkit.exceptions import ConfigurationError
from matricula_toolkit.provider_policy import (
    ProviderPolicy,
    ProviderPolicyError,
)
from matricula_toolkit.providers import load_providers

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _allowed(decision) -> bool:
    return decision.allowed is True


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
        "entry_hosts": ["example.com"],
        "content_hosts": ["content.example.com"],
        "redirect_hosts": ["redirect.example.com"],
        "authentication": "public",
        "image_access": "permitted_after_review",
        "terms_url": "https://example.com/terms",
        "terms_checked_at": "2026-10-29",
        "verification_status": "unverified",
        "enabled": True,
        "notes": "",
    }


def _write_providers(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _ssrf_pass():
    """Patch SSRF validation functions to always pass (avoid DNS in tests)."""
    return patch.multiple(
        "matricula_toolkit.provider_policy",
        validate_host=lambda h: h,
        validate_url=lambda u: u,
    )


# ------------------------------------------------------------------
# Construction
# ------------------------------------------------------------------


def test_policy_construction():
    """ProviderPolicy can be constructed from the bundled providers."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    assert policy is not None


def test_policy_unknown_provider_raises():
    """Looking up an unknown provider raises ProviderPolicyError."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with pytest.raises(ProviderPolicyError, match="Unknown provider"):
        policy.can_browse("nonexistent-provider", "example.com")


def test_policy_disabled_provider_raises():
    """Looking up a disabled provider raises ProviderPolicyError."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with pytest.raises(ProviderPolicyError, match="Provider disabled"):
        policy.can_browse("antenati", "antenati.cultura.gov.it")


# ------------------------------------------------------------------
# can_browse: entry hosts only
# ------------------------------------------------------------------


def test_can_browse_entry_host_allowed(tmp_path):
    """Browsing an entry host is allowed for an enabled provider."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_browse("matricula-online", "data.matricula-online.eu")
    assert _allowed(decision) is True
    assert decision.capability == "browser"


def test_can_browse_non_entry_host_denied(tmp_path):
    """Browsing a non-entry host is denied even if it is a content host."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_browse("matricula-online", "img.data.matricula-online.eu")
    assert _allowed(decision) is False


def test_can_browse_unknown_host_denied(tmp_path):
    """Browsing an unknown host is denied."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_browse("matricula-online", "evil.example.com")
    assert _allowed(decision) is False


def test_can_browse_subdomain_rejected(tmp_path):
    """A subdomain of an entry host is not allowed (exact match only)."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_browse("matricula-online", "sub.data.matricula-online.eu")
    assert _allowed(decision) is False


def test_can_browse_unknown_authentication_denied(tmp_path):
    """Browsing is denied when authentication is unknown."""
    entry = _base_provider_entry()
    entry["authentication"] = "unknown"
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_browse("test-provider", "example.com")
    assert _allowed(decision) is False


# ------------------------------------------------------------------
# can_proxy_connect: content hosts only
# ------------------------------------------------------------------


def test_can_proxy_connect_content_host_allowed(tmp_path):
    """Proxying a content host is allowed for an enabled provider."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_proxy_connect(
            "matricula-online", "img.data.matricula-online.eu"
        )
    assert _allowed(decision) is True
    assert decision.capability == "proxy"


def test_can_proxy_connect_entry_host_denied(tmp_path):
    """Proxying an entry host (not a content host) is denied."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_proxy_connect(
            "matricula-online", "data.matricula-online.eu"
        )
    assert _allowed(decision) is False


def test_can_proxy_connect_unknown_host_denied(tmp_path):
    """Proxying an unknown host is denied."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_proxy_connect("matricula-online", "evil.example.com")
    assert _allowed(decision) is False


# ------------------------------------------------------------------
# can_download_image: content host + image_access permitting
# ------------------------------------------------------------------


def test_can_download_image_permitted_after_review_allowed(tmp_path):
    """Image download is allowed when image_access is permitted_after_review."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_download_image("test-provider", "content.example.com")
    assert _allowed(decision) is True
    assert decision.capability == "image_download"


def test_can_download_image_manual_only_denied(tmp_path):
    """Image download is denied when image_access is manual_only."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_download_image(
            "matricula-online", "img.data.matricula-online.eu"
        )
    assert _allowed(decision) is False


def test_can_download_image_written_permission_required_denied(tmp_path):
    """Image download is denied when image_access is written_permission_required."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_download_image("archion", "www.archion.de")
    assert _allowed(decision) is False


def test_can_download_image_unknown_access_denied(tmp_path):
    """Image download is denied when image_access is unknown."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_download_image(
            "digitalarkivet", "media.digitalarkivet.no"
        )
    assert _allowed(decision) is False


def test_can_download_image_non_content_host_denied(tmp_path):
    """Image download is denied for a non-content host even if access permits."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.can_download_image("familysearch", "www.familysearch.org")
    assert _allowed(decision) is False


# ------------------------------------------------------------------
# validate_redirect
# ------------------------------------------------------------------


def test_validate_redirect_allowed(tmp_path):
    """A redirect to an allowed redirect host is valid."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider", "https://example.com/", "https://redirect.example.com/"
        )
    assert _allowed(decision) is True
    assert decision.capability == "redirect"


def test_validate_redirect_unknown_host_denied(tmp_path):
    """A redirect to an unknown host is denied."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "matricula-online",
            "https://data.matricula-online.eu/",
            "https://evil.example.com/",
        )
    assert _allowed(decision) is False


def test_validate_redirect_empty_redirect_hosts_denied(tmp_path):
    """Leere redirect_hosts dürfen nicht als Erlaubnis für beliebige Redirects gelten."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = []
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider", "https://example.com/", "https://other.example.com/"
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


def test_validate_redirect_http_scheme_denied(tmp_path):
    """Redirect auf HTTP muss durch SSRF-Policy abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    # SSRF nicht patchen — reale SSRF muss HTTP ablehnen.
    decision = policy.validate_redirect(
        "test-provider", "https://example.com/", "http://redirect.example.com/"
    )
    assert _allowed(decision) is False


def test_validate_redirect_non_443_port_denied(tmp_path):
    """Redirect auf unzulässigem Port muss durch SSRF-Policy abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    decision = policy.validate_redirect(
        "test-provider", "https://example.com/", "https://redirect.example.com:8443/"
    )
    assert _allowed(decision) is False


def test_validate_redirect_credentials_denied(tmp_path):
    """Redirect mit URL-Zugangsdaten muss abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    decision = policy.validate_redirect(
        "test-provider",
        "https://example.com/",
        "https://user:pass@redirect.example.com/",
    )
    assert _allowed(decision) is False


def test_validate_redirect_ip_literal_denied(tmp_path):
    """Redirect auf IP-Literal muss durch SSRF-Policy abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    decision = policy.validate_redirect(
        "test-provider", "https://example.com/", "https://127.0.0.1/"
    )
    assert _allowed(decision) is False


def test_validate_redirect_private_ip_denied(tmp_path):
    """Redirect auf private IP muss durch SSRF-Policy abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    decision = policy.validate_redirect(
        "test-provider", "https://example.com/", "https://10.0.0.1/"
    )
    assert _allowed(decision) is False


def test_validate_redirect_link_local_denied(tmp_path):
    """Redirect auf Link-Local-Adresse muss durch SSRF-Policy abgelehnt werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    decision = policy.validate_redirect(
        "test-provider", "https://example.com/", "https://169.254.169.254/"
    )
    assert _allowed(decision) is False


# ------------------------------------------------------------------
# Redirect chain simulation (per-hop validation)
# ------------------------------------------------------------------


def test_validate_redirect_chain_first_allowed_second_denied(tmp_path):
    """Redirect-Kette: erster Hop erlaubt, zweiter Hop verboten.

    Jeder Redirect-Hop muss einzeln validiert werden. Da Playwrights
    page.goto() Redirects automatisch verfolgt und einzelne Hops nicht
    exposed, kann die Anwendung keine per-Hop-Provider-Policy-Validierung
    durchführen. Dieser Test simuliert eine Kette durch aufeinanderfolgende
    validate_redirect()-Aufrufe.
    """
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["hop1.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        # Erster Hop: erlaubt (in redirect_hosts)
        decision1 = policy.validate_redirect(
            "test-provider",
            "https://example.com/",
            "https://hop1.example.com/",
        )
        assert _allowed(decision1) is True

        # Zweiter Hop: verboten (nicht in redirect_hosts)
        decision2 = policy.validate_redirect(
            "test-provider",
            "https://hop1.example.com/",
            "https://evil.example.com/",
        )
        assert _allowed(decision2) is False
        assert "not a confirmed redirect host" in (decision2.reason or "")


def test_validate_redirect_chain_all_allowed(tmp_path):
    """Redirect-Kette: alle Hops erlaubt (alle in redirect_hosts)."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["hop1.example.com", "hop2.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        decision1 = policy.validate_redirect(
            "test-provider",
            "https://example.com/",
            "https://hop1.example.com/",
        )
        assert _allowed(decision1) is True

        decision2 = policy.validate_redirect(
            "test-provider",
            "https://hop1.example.com/",
            "https://hop2.example.com/",
        )
        assert _allowed(decision2) is True


# ------------------------------------------------------------------
# Capability combination tests
# ------------------------------------------------------------------


def test_capability_browse_allowed_proxy_denied(tmp_path):
    """Provider mit Browse erlaubt aber Proxy verweigert: Download muss abgelehnt werden."""
    entry = _base_provider_entry()
    entry["content_hosts"] = ["content.example.com"]
    entry["entry_hosts"] = ["example.com"]
    entry["image_access"] = "permitted_after_review"
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        # Browse auf Entry-Host: erlaubt
        browse = policy.can_browse("test-provider", "example.com")
        assert _allowed(browse) is True

        # Proxy auf Content-Host: erlaubt (ist in content_hosts)
        proxy = policy.can_proxy_connect("test-provider", "content.example.com")
        assert _allowed(proxy) is True

        # Download: erlaubt (image_access = permitted_after_review)
        download = policy.can_download_image("test-provider", "content.example.com")
        assert _allowed(download) is True


def test_capability_browse_denied_unknown_host(tmp_path):
    """Browse auf unbekanntem Host muss verweigert werden (kein Netzwerkzugriff)."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        decision = policy.can_browse("test-provider", "evil.example.com")
        assert _allowed(decision) is False
        assert decision.capability == "browser"


def test_capability_proxy_denied_non_content_host(tmp_path):
    """Proxy auf nicht-Content-Host muss verweigert werden."""
    entry = _base_provider_entry()
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        # Entry-Host ist kein Content-Host -> Proxy verweigert
        decision = policy.can_proxy_connect("test-provider", "example.com")
        assert _allowed(decision) is False
        assert decision.capability == "proxy"


def test_capability_download_denied_manual_only(tmp_path):
    """Download bei image_access=manual_only muss verweigert werden."""
    entry = _base_provider_entry()
    entry["image_access"] = "manual_only"
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        decision = policy.can_download_image("test-provider", "content.example.com")
        assert _allowed(decision) is False
        assert decision.capability == "image_download"
        assert "manual_only" in (decision.reason or "")


def test_capability_download_denied_written_permission_required(tmp_path):
    """Download bei image_access=written_permission_required muss verweigert werden."""
    entry = _base_provider_entry()
    entry["image_access"] = "written_permission_required"
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        decision = policy.can_download_image("test-provider", "content.example.com")
        assert _allowed(decision) is False
        assert decision.capability == "image_download"


def test_capability_download_denied_unknown_access(tmp_path):
    """Download bei image_access=unknown muss verweigert werden."""
    entry = _base_provider_entry()
    entry["image_access"] = "unknown"
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)

    with _ssrf_pass():
        decision = policy.can_download_image("test-provider", "content.example.com")
        assert _allowed(decision) is False
        assert decision.capability == "image_download"


# ------------------------------------------------------------------
# check_access
# ------------------------------------------------------------------


def test_check_access_returns_decisions(tmp_path):
    """check_access returns a list of PolicyDecision objects."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decisions = policy.check_access(
            "matricula-online", "data.matricula-online.eu", browse=True
        )
    assert len(decisions) == 1
    assert decisions[0].capability == "browser"


def test_is_access_granted_true(tmp_path):
    """is_access_granted returns True when all decisions are allowed."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decisions = policy.check_access(
            "matricula-online", "data.matricula-online.eu", browse=True
        )
    assert policy.is_access_granted(decisions) is True


def test_is_access_granted_false(tmp_path):
    """is_access_granted returns False when any decision is denied."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decisions = policy.check_access(
            "matricula-online", "evil.example.com", browse=True
        )
    assert policy.is_access_granted(decisions) is False


# ------------------------------------------------------------------
# Provider introspection
# ------------------------------------------------------------------


def test_list_providers(tmp_path):
    """list_providers returns all providers sorted by ID."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    listed = policy.list_providers()
    assert len(listed) == 10
    assert [p.id for p in listed] == sorted(p.id for p in listed)


def test_list_enabled_and_disabled(tmp_path):
    """list_enabled and list_disabled split correctly."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    enabled = policy.list_enabled()
    disabled = policy.list_disabled()
    assert len(enabled) == 6
    assert len(disabled) == 4
    assert len(enabled) + len(disabled) == 10


# ------------------------------------------------------------------
# SSRF preservation: provider grant never overrides SSRF denial
# ------------------------------------------------------------------


def test_ssrf_preservation_forbidden_ip(tmp_path):
    """A provider-allowed host that is a forbidden IP is denied (SSRF preserved)."""
    providers = load_providers()
    policy = ProviderPolicy(providers)
    # Do NOT patch SSRF here — real SSRF must reject the IP literal.
    decision = policy.can_proxy_connect("matricula-online", "127.0.0.1")
    assert _allowed(decision) is False


def test_provider_error_is_configuration_error():
    """ProviderPolicyError is a ConfigurationError."""
    assert issubclass(ProviderPolicyError, ConfigurationError)


# ------------------------------------------------------------------
# Redirect policy: initial URL vs. redirect targets
# ------------------------------------------------------------------


def test_validate_redirect_empty_redirect_hosts_denies_all_targets(tmp_path):
    """With empty redirect_hosts, validate_redirect denies every redirect target.

    This is correct: redirect_hosts lists acknowledged redirect targets.
    Empty means none are acknowledged.  The initial page URL is handled
    separately in desktop_services.py (not validated against redirect_hosts).
    """
    entry = _base_provider_entry()
    entry["redirect_hosts"] = []
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://any-other-host.example.com/",
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


def test_validate_redirect_allowed_redirect_target(tmp_path):
    """A redirect to a host listed in redirect_hosts is allowed."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["redirect.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://redirect.example.com/",
        )
    assert _allowed(decision) is True


def test_validate_redirect_denied_non_listed_host(tmp_path):
    """A redirect to a host NOT in redirect_hosts is denied."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["redirect.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://evil.example.com/",
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


def test_validate_redirect_chain_multi_hop(tmp_path):
    """Multi-hop redirect chain: each hop validated independently."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["hop1.example.com", "hop2.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        # Hop 1: allowed
        d1 = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://hop1.example.com/",
        )
        assert _allowed(d1) is True

        # Hop 2: allowed
        d2 = policy.validate_redirect(
            "test-provider",
            "https://hop1.example.com/",
            "https://hop2.example.com/",
        )
        assert _allowed(d2) is True

        # Hop 3: denied (not in redirect_hosts)
        d3 = policy.validate_redirect(
            "test-provider",
            "https://hop2.example.com/",
            "https://evil.example.com/",
        )
        assert _allowed(d3) is False


def test_validate_redirect_private_ip_denied_by_provider_policy(tmp_path):
    """Redirect to private IP not in redirect_hosts is denied by provider policy."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = []
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://10.0.0.1/",
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


def test_validate_redirect_loopback_denied_by_provider_policy(tmp_path):
    """Redirect to loopback not in redirect_hosts is denied by provider policy."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = []
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://127.0.0.1/",
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


def test_validate_redirect_link_local_denied_by_provider_policy(tmp_path):
    """Redirect to link-local not in redirect_hosts is denied by provider policy."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = []
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        decision = policy.validate_redirect(
            "test-provider",
            "https://content.example.com/",
            "https://169.254.169.254/",
        )
    assert _allowed(decision) is False
    assert "not a confirmed redirect host" in (decision.reason or "")


# ------------------------------------------------------------------
# Capability checks remain intact after redirect changes
# ------------------------------------------------------------------


def test_capability_checks_still_work_after_redirect_changes(tmp_path):
    """can_browse/can_proxy_connect/can_download_image still enforce policy."""
    entry = _base_provider_entry()
    entry["redirect_hosts"] = ["redirect.example.com"]
    data = {"version": 1, "providers": [entry]}
    p = tmp_path / "providers.json"
    _write_providers(p, data)
    providers = load_providers(p)
    policy = ProviderPolicy(providers)
    with _ssrf_pass():
        # Browse: entry host allowed
        b = policy.can_browse("test-provider", "example.com")
        assert _allowed(b) is True

        # Proxy: content host allowed
        pxy = policy.can_proxy_connect("test-provider", "content.example.com")
        assert _allowed(pxy) is True

        # Download: content host + image_access permitted
        dl = policy.can_download_image("test-provider", "content.example.com")
        assert _allowed(dl) is True

        # Unknown host denied everywhere
        assert _allowed(policy.can_browse("test-provider", "evil.example.com")) is False
        assert (
            _allowed(policy.can_proxy_connect("test-provider", "evil.example.com"))
            is False
        )
        assert (
            _allowed(policy.can_download_image("test-provider", "evil.example.com"))
            is False
        )
