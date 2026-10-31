# Provider Configuration

The Matricula Toolkit uses a machine-readable, extensible provider configuration
for external genealogical archives. Provider data is kept strictly separate from
the SSRF security logic in `security.py` and `local_proxy.py`. Both checks must
pass for access to be granted; a provider grant never overrides an SSRF denial.

## Files

| File | Purpose |
|------|---------|
| `src/matricula_toolkit/providers.json` | Provider data (10 archives) |
| `src/matricula_toolkit/providers.py` | Provider dataclass, loading, validation |
| `src/matricula_toolkit/provider_policy.py` | Three-capability access policy with SSRF delegation |

## Architecture

### Separation of concerns

- **`providers.json` / `providers.py`** — Provider metadata: IDs, names, hosts,
  viewer types, authentication, image access rights, verification status, enabled
  flag. Pure data; no security logic.
- **`security.py` / `local_proxy.py`** — Centralized SSRF policy: HTTPS only,
  port 443 only, no credentials, no IP literals, DNS rebinding protection.
- **`provider_policy.py`** — Provider-level access policy: three independent
  capabilities (browser, proxy, image download), each checked against the
  provider's declared hosts and access classes, then delegated to SSRF.

### Three capabilities (independently checked)

| Capability | Allowed hosts | Additional condition |
|------------|---------------|----------------------|
| Browser navigation | `entry_hosts` only | Authentication must not be `unknown` |
| Proxy connection | `content_hosts` only | None (SSRF still applies) |
| Automated image download | `content_hosts` only | `image_access` must be `permitted_after_review` |

A provider grant never overrides an SSRF denial. Both checks must pass.

### Closed vocabularies

| Field | Allowed values |
|-------|---------------|
| `provider_type` | `scan_archive`, `search_index`, `catalog` |
| `viewer_type` | `unknown`, `html_viewer`, `tile_viewer`, `iiif`, `direct_image`, `index_only` |
| `authentication` | `public`, `account_required`, `paid_subscription`, `collection_restricted`, `unknown` |
| `image_access` | `manual_only`, `permitted_after_review`, `written_permission_required`, `unknown` |
| `verification_status` | `unverified`, `partially_verified`, `verified` |

### Security rules

- Providers are **disabled by default** (`enabled: false`).
- Unknown hosts, unconfirmed redirect targets, and unknown viewer properties
  are never automatically enabled.
- No wildcards, no IP literals, no URL credentials.
- HTTPS + port 443 only.
- Exact host matching only — subdomains are not allowed.
- Redirects within HTTPS tunnels are not claimed as fully controllable; each
  redirect target is independently validated.
- `manual_only` and `written_permission_required` deny automated image download.
- Only `permitted_after_review` permits automated image download.

## Current providers

### Enabled (6)

| ID | Name | Verification | Viewer | Auth | Image access |
|----|------|-------------|--------|------|--------------|
| `matricula-online` | Matricula Online | verified | html_viewer | public | manual_only |
| `digitalarkivet` | Digitalarkivet (NO) | partially_verified | html_viewer | public | manual_only |
| `raduraksti` | Raduraksti (LV) | partially_verified | html_viewer | account_required | manual_only |
| `jewishgen` | JewishGen | partially_verified | index_only | public | unknown |
| `archion` | ARCHION (DE) | partially_verified | unknown | paid_subscription | written_permission_required |
| `familysearch` | FamilySearch | partially_verified | unknown | account_required | manual_only |

### Disabled (4 — unverified)

| ID | Name | Reason |
|----|------|--------|
| `antenati` | Portale Antenati (IT) | Official site not fully reachable (403); hosts and terms unverified |
| `genbaza` | GenBaza (PL) | Not yet cleared for automated use |
| `arkivalieronline` | Arkivalieronline (DK) | Official site not reachable (transport error) |
| `slave-societies-digital-archive` | Slave Societies Digital Archive | Official site not resolvable; no hosts verified |

## Adding a new provider

1. Add an entry to `src/matricula_toolkit/providers.json` with all required fields
   (see `_REQUIRED_FIELDS` in `providers.py`).
2. Use only values from the closed vocabularies above.
3. Set `enabled: false` until the provider has been verified.
4. Run `python3 -m pytest tests/test_providers.py` to validate loading.
5. Run `python3 -m ruff check` and `python3 -m mypy src/` to verify code quality.

## Verifying hosts and redirects

Before enabling a provider:

1. **Entry hosts** — Confirm the archive's main website host(s). These are the
   only hosts allowed for browser navigation.
2. **Content hosts** — Confirm the host(s) serving images or API content. These
   are the only hosts allowed for proxy connections and image download.
3. **Redirect hosts** — If the archive redirects to other hosts (e.g. for login
   or image delivery), list them here. Each redirect target is independently
   validated against SSRF.
4. **Terms** — Record the terms URL and the date they were checked
   (`terms_checked_at`).
5. **Verification** — Set `verification_status` to `verified` or
   `partially_verified` only after confirming hosts and access rights.

## When to enable a provider

Enable a provider (`enabled: true`) only when:

- All hosts have been confirmed reachable and correct.
- The viewer type, authentication, and image access have been determined.
- The terms have been reviewed and recorded.
- You accept the access class restrictions (e.g. `manual_only` means no
  automated image download).

## Safe testing

- Tests use mocked SSRF validation (`validate_host`/`validate_url` patched to
  pass) to avoid DNS dependencies.
- The `test_ssrf_preservation_forbidden_ip` test verifies that real SSRF
  validation still rejects IP literals even when a provider lists them.
- Disabled providers raise `ProviderPolicyError` on any access attempt — they
  cannot be accidentally used.

## API reference

### `load_providers(path=None)`

Load and validate all providers from the JSON configuration. Returns a
`dict[str, Provider]`. Raises `ProviderError` on invalid configuration.

### `ProviderPolicy(providers)`

Construct a policy from a provider mapping. Methods:

- `can_browse(provider_id, host)` — Browser navigation check
- `can_proxy_connect(provider_id, host)` — Proxy connection check
- `can_download_image(provider_id, host)` — Automated image download check
- `validate_redirect(provider_id, original_url, redirect_url)` — Redirect target validation
- `check_access(provider_id, host, *, browse=False, proxy=False, download_image=False)` — Combined check
- `is_access_granted(decisions)` — True if all decisions are allowed
- `list_providers()` / `list_enabled()` / `list_disabled()` — Introspection

All methods return `PolicyDecision(allowed, provider_id, host, capability, reason)`.

### Exceptions

- `ProviderError` — Configuration errors (subclass of `ConfigurationError`)
- `ProviderPolicyError` — Policy check failures (subclass of `ConfigurationError`)
