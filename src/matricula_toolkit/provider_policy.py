"""Provider policy for access control to external genealogical archives.

This module implements a provider-level access policy that works *alongside*
(never instead of) the SSRF security policy in :mod:`matricula_toolkit.security`
and :mod:`matricula_toolkit.local_proxy`.

Three capabilities are checked independently:

* **Browser navigation** -- opening the archive website in a browser.
  Allowed only against exact ``entry_hosts``.
* **Proxy connections** -- routing through the local SSRF proxy.
  Allowed only against exact ``content_hosts``.
* **Automated image download** -- programmatically fetching image bytes.
  Allowed only against exact ``content_hosts`` and only when the
  provider's ``image_access`` permits it (``manual_only`` and
  ``written_permission_required`` are denied for automated download).

A provider grant never overrides a ``security.py`` denial.  Both checks
must pass for access to be granted.  Providers are disabled by default;
unknown hosts, unconfirmed redirect targets and unknown viewer properties
are never automatically enabled.
"""

from __future__ import annotations

from dataclasses import dataclass

from matricula_toolkit.exceptions import ConfigurationError
from matricula_toolkit.providers import Provider
from matricula_toolkit.security import SecurityError, validate_host, validate_url

# Access classes that permit automated image download.
_AUTOMATED_DOWNLOAD_ALLOWED: frozenset[str] = frozenset({"permitted_after_review"})

# Access classes that permit browser navigation (read-only, manual use).
_BROWSER_ALLOWED_AUTHENTICATION: frozenset[str] = frozenset(
    {"public", "account_required", "paid_subscription", "collection_restricted"}
)

# Authentication classes that are never sufficient on their own.
_UNKNOWN_AUTHENTICATION: frozenset[str] = frozenset({"unknown"})


class ProviderPolicyError(ConfigurationError):
    """Raised when a provider policy check fails."""


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a provider policy check."""

    allowed: bool
    provider_id: str
    host: str
    capability: str
    reason: str | None = None


class ProviderPolicy:
    """Provider-level access policy for genealogical archives.

    The policy is constructed from a mapping of provider ID to
    :class:`~matricula_toolkit.providers.Provider`.  All checks are
    exact-match (no wildcards) and are combined with the centralized SSRF
    security policy.
    """

    def __init__(self, providers: dict[str, Provider]) -> None:
        self._providers = providers

    # ------------------------------------------------------------------
    # Provider lookup
    # ------------------------------------------------------------------

    def _require_provider(self, provider_id: str) -> Provider:
        """Return the provider for *provider_id*, raising if unknown or disabled."""
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderPolicyError(f"Unknown provider: {provider_id!r}")
        if not provider.enabled:
            raise ProviderPolicyError(f"Provider disabled: {provider_id!r}")
        return provider

    # ------------------------------------------------------------------
    # SSRF delegation (always applied, never replaced by provider grant)
    # ------------------------------------------------------------------

    def _ssrf_check(self, host: str) -> None:
        """Run the centralized SSRF host validation.  Raises on denial."""
        try:
            validate_host(host)
        except SecurityError as exc:
            raise ProviderPolicyError(
                f"SSRF policy rejected host {host!r}: {exc}"
            ) from exc

    def _ssrf_url_check(self, url: str) -> None:
        """Run the centralized SSRF URL validation.  Raises on denial."""
        try:
            validate_url(url)
        except SecurityError as exc:
            raise ProviderPolicyError(
                f"SSRF policy rejected URL {url!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Capability: browser navigation
    # ------------------------------------------------------------------

    def can_browse(self, provider_id: str, host: str) -> PolicyDecision:
        """Return whether the browser may navigate to *host* for *provider_id*.

        Browser navigation is allowed only against exact ``entry_hosts``
        and only when the provider's authentication class permits it.
        """
        provider = self._require_provider(provider_id)

        if host not in provider.entry_hosts:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "browser",
                f"Host {host!r} is not an entry host of provider {provider_id!r}",
            )

        if provider.authentication in _UNKNOWN_AUTHENTICATION:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "browser",
                f"Provider {provider_id!r} has unknown authentication; browser navigation denied",
            )

        if provider.authentication not in _BROWSER_ALLOWED_AUTHENTICATION:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "browser",
                f"Provider {provider_id!r} authentication {provider.authentication!r} does not permit browser navigation",
            )

        # SSRF check is mandatory and cannot be overridden by the provider grant.
        self._ssrf_check(host)

        return PolicyDecision(True, provider_id, host, "browser")

    # ------------------------------------------------------------------
    # Capability: proxy connection
    # ------------------------------------------------------------------

    def can_proxy_connect(self, provider_id: str, host: str) -> PolicyDecision:
        """Return whether the local proxy may connect to *host* for *provider_id*.

        Proxy connections are allowed only against exact ``content_hosts``.
        """
        provider = self._require_provider(provider_id)

        if host not in provider.content_hosts:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "proxy",
                f"Host {host!r} is not a content host of provider {provider_id!r}",
            )

        # SSRF check is mandatory and cannot be overridden by the provider grant.
        self._ssrf_check(host)

        return PolicyDecision(True, provider_id, host, "proxy")

    # ------------------------------------------------------------------
    # Capability: automated image download
    # ------------------------------------------------------------------

    def can_download_image(self, provider_id: str, host: str) -> PolicyDecision:
        """Return whether images may be automatically downloaded from *host*.

        Allowed only against exact ``content_hosts`` and only when the
        provider's ``image_access`` permits automated download.
        ``manual_only`` and ``written_permission_required`` are denied.
        """
        provider = self._require_provider(provider_id)

        if host not in provider.content_hosts:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "image_download",
                f"Host {host!r} is not a content host of provider {provider_id!r}",
            )

        if provider.image_access not in _AUTOMATED_DOWNLOAD_ALLOWED:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "image_download",
                f"Provider {provider_id!r} image_access {provider.image_access!r} does not permit automated image download",
            )

        # SSRF check is mandatory and cannot be overridden by the provider grant.
        self._ssrf_check(host)

        return PolicyDecision(True, provider_id, host, "image_download")

    # ------------------------------------------------------------------
    # Redirect target validation
    # ------------------------------------------------------------------

    def validate_redirect(
        self, provider_id: str, original_url: str, redirect_url: str
    ) -> PolicyDecision:
        """Validate a redirect target URL for *provider_id*.

        The redirect target must be an exact ``redirect_host`` of the
        provider and must pass the SSRF URL validation.  Redirects within
        an HTTPS tunnel are not claimed to be fully controllable at the
        application level; each target is independently validated.
        """
        provider = self._require_provider(provider_id)

        from urllib.parse import urlparse

        try:
            parsed = urlparse(redirect_url)
        except ValueError as exc:
            return PolicyDecision(
                False,
                provider_id,
                redirect_url,
                "redirect",
                f"Invalid redirect URL: {exc}",
            )

        host = parsed.hostname
        if not host:
            return PolicyDecision(
                False,
                provider_id,
                redirect_url,
                "redirect",
                "Redirect URL has no hostname",
            )

        if host not in provider.redirect_hosts:
            return PolicyDecision(
                False,
                provider_id,
                host,
                "redirect",
                f"Host {host!r} is not a confirmed redirect host of provider {provider_id!r}",
            )

        # SSRF check on the redirect target is mandatory.
        try:
            self._ssrf_url_check(redirect_url)
        except ProviderPolicyError as exc:
            return PolicyDecision(
                False,
                provider_id,
                host or redirect_url,
                "redirect",
                str(exc),
            )

        return PolicyDecision(True, provider_id, host, "redirect")

    # ------------------------------------------------------------------
    # Combined access check (all applicable capabilities)
    # ------------------------------------------------------------------

    def check_access(
        self,
        provider_id: str,
        host: str,
        *,
        browse: bool = False,
        proxy: bool = False,
        download_image: bool = False,
    ) -> list[PolicyDecision]:
        """Run all requested capability checks and return their decisions.

        If any check is denied, the overall access is denied.  SSRF
        validation is applied to every check that involves a network host.
        """
        decisions: list[PolicyDecision] = []

        if browse:
            decisions.append(self.can_browse(provider_id, host))
        if proxy:
            decisions.append(self.can_proxy_connect(provider_id, host))
        if download_image:
            decisions.append(self.can_download_image(provider_id, host))

        return decisions

    def is_access_granted(self, decisions: list[PolicyDecision]) -> bool:
        """Return True only if every decision is allowed."""
        return all(d.allowed for d in decisions)

    # ------------------------------------------------------------------
    # Provider introspection
    # ------------------------------------------------------------------

    def list_providers(self) -> list[Provider]:
        """Return all configured providers, sorted by ID."""
        return sorted(self._providers.values(), key=lambda p: p.id)

    def list_enabled(self) -> list[Provider]:
        """Return all enabled providers, sorted by ID."""
        return sorted(
            (p for p in self._providers.values() if p.enabled),
            key=lambda p: p.id,
        )

    def list_disabled(self) -> list[Provider]:
        """Return all disabled providers, sorted by ID."""
        return sorted(
            (p for p in self._providers.values() if not p.enabled),
            key=lambda p: p.id,
        )
