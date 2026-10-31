# Matricula Toolkit

CLI, desktop workbench, and MCP server for downloading Matricula church book scans
([matricula-online.eu](https://data.matricula-online.eu)), with an extensible,
machine-readable provider configuration for external genealogical archives.

The Matricula Toolkit helps genealogists and family historians download high-resolution
church book scans from Matricula-online and other archives, verify them, and prepare
them for OCR/HTR (Optical / Handwritten Text Recognition) with the Kraken backend —
all locally, with no cloud services or paid APIs.

Provider data (archive hosts, access classes, verification status) is kept strictly
separate from the SSRF security logic. Both the provider policy and the centralized
SSRF policy must pass for any network access to be granted.

## Features

- **CLI** (`matricula-download`): download single pages or page ranges, verify JPEG
  integrity, diagnose download errors
- **Desktop workbench** (`matricula-desktop`, optional PySide6): dark-themed GUI with
  dashboard, research, books, image browser, hit checking, sources, OCR jobs, system
  monitoring, installer, settings, and help sections
- **MCP server** (`matricula-mcp`): 7 tools for integration with MCP-compatible clients
  (`inspect_book`, `inspect_page`, `download_page`, `download_range`, `verify_image`,
  `diagnose_error`, `prepare_ocr`)
- **Automatic installer** (`matricula-install`): OS detection, dependency checks,
  venv setup, Kraken OCR backend, and model management
- **Extensible provider configuration** (`providers.json`): machine-readable
  configuration for 10 external genealogical archives, separated from SSRF security
  logic; three capabilities (browser navigation, proxy connection, automated image
  download) checked independently
- **SSRF protection**: deny-by-default policy (HTTPS only, port 443 only, no
  credentials, no IP literals, DNS rebinding protection) enforced by `security.py`
  and `local_proxy.py`
- **Resume support**: skips already-downloaded files
- **JPEG verification**: full Pillow decode, not just magic bytes
- **Atomic saving**: temp file + `os.replace`
- **Persistent SQLite storage**: source records in the platform-appropriate user data
  directory (XDG / APPDATA / Application Support)
- **System monitoring**: CPU, RAM, and CPU temperature (Linux) with OCR load protection
- **Configuration**: environment variables or `.env` file via `python-dotenv`
- **Local processing only**: all downloads and OCR run on your machine

## Screenshots

No screenshots are currently included in the repository. Contributions of screenshots
showing the desktop workbench are welcome. Place them under `docs/screenshots/` and
reference them here.

## Requirements

- Python 3.10 or higher
- Playwright/Chromium browser (installed via `playwright install chromium`)
- Optional: PySide6 for the desktop workbench (`pip install ".[desktop]"`)
- Optional: Kraken OCR backend and compatible `.mlmodel` models for OCR/HTR
- Operating systems: Linux, macOS, Windows (desktop GUI validated on Linux; platform
  paths are implemented for all three)

## Installation

### From source

```bash
git clone https://github.com/shadow396-creator/matricula-toolkit.git
cd matricula-toolkit
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
playwright install chromium
```

### Installation variants

**CLI only** (no desktop GUI):

```bash
pip install -e .
```

**CLI + Desktop workbench** (requires PySide6):

```bash
pip install -e ".[desktop]"
```

**MCP server** (same package, no extra dependencies beyond the core):

```bash
pip install -e .
# Then run:
python -m matricula_toolkit.mcp_server
```

**Development** (includes test and lint dependencies):

```bash
pip install -e ".[dev]"
```

### Automatic installer

The toolkit includes an installation assistant that detects your operating system,
checks for missing prerequisites, sets up a virtual environment, installs Kraken, and
verifies the installation. It is idempotent: re-running it does not damage a working
installation.

```bash
# Check what is missing (no changes)
matricula-install --check-only

# Full interactive installation
matricula-install

# Non-interactive installation (CI / automation)
matricula-install --non-interactive

# Skip virtual environment creation
matricula-install --no-venv

# Skip Kraken OCR backend
matricula-install --no-kraken

# Skip model download
matricula-install --no-models
```

The installer detects OS, distribution, CPU architecture, Python version, RAM, disk
space, and GPU support; checks for existing Python packages and system tools; installs
missing system packages via your distribution's package manager (never silently with
`sudo` for every step); creates an isolated Python virtual environment; installs
required Python packages and the Playwright Chromium browser; optionally downloads
Kraken OCR models; and verifies the installation.

No cloud services are used. All processing runs locally.

## Quick start

Download a single page:

```bash
matricula-download page 104 \
  --url "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/" \
  --book 01519 \
  --out ./downloads
```

Download a range of pages:

```bash
matricula-download range 104 303 \
  --url "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/" \
  --book 01519 \
  --out ./downloads \
  --wait 5
```

Verify downloaded images:

```bash
matricula-download verify ./downloads
```

Diagnose a page:

```bash
matricula-download diagnose 104 \
  --url "https://data.matricula-online.eu/de/slovenia/maribor/maribor-sv-janez-krstnik/01519/"
```

## Desktop application

Launch the optional desktop workbench (requires `.[desktop]` extra):

```bash
matricula-desktop
```

Available sections:

- **Dashboard** — overview and status
- **Research** — book validation, single-page and range downloads
- **Books** — image list with file sizes, open in image browser
- **Image browser** — scrollable image view, zoom, previous/next, dimensions and file info
- **Hit checking** — side-by-side original vs. correction comparison
- **Transcription correction** (formerly `Transkription korrigieren`) — manual
  transcription correction: click words on
  church book images, correct them, and apply corrections position-accurately to the
  full transcription with persistent undo/redo
- **Sources** — download history with success/error status, verify all, refresh
- **OCR jobs** — Kraken backend detection, local model search, job execution, job list
- **System** — CPU, RAM, temperature monitoring with 5-second auto-refresh
- **Installer** — automatic installation assistant
- **Settings** — persistent JSON settings, platform-appropriate paths
- **Help** — documentation of all sections and OCR preparation limits

The application exits gracefully with a clear message when PySide6 is not installed.

**System monitoring:** The status bar shows CPU utilisation, RAM usage, and CPU
temperature (Linux only). The first CPU reading returns "unknown" (baseline); subsequent
readings use a two-sample delta from `/proc/stat`.

**OCR load protection:** Before starting an OCR job, the current resource snapshot is
compared against configurable thresholds (`ocr_max_cpu_percent`, `ocr_max_ram_percent`,
`ocr_max_temperature_c`). If any threshold is exceeded, the job is blocked with a
descriptive reason. Unknown sensor values (e.g. no CPU temperature sensor) are treated
as "not available" and block the job when the corresponding limit is configured.

**SQLite persistence:** Source records are stored in a SQLite database at the
platform-appropriate user data directory:

- Linux: `$XDG_DATA_HOME/matricula-toolkit/sources.db`
  (default: `~/.local/share/matricula-toolkit/sources.db`)
- Windows: `%APPDATA%/MatriculaToolkit/sources.db`
- macOS: `~/Library/Application Support/MatriculaToolkit/sources.db`

Sources survive application restarts and are shared across service instances.

### Interface languages

The desktop workbench is available in five languages:

| Code | Language | Native name |
| ---- | -------- | ----------- |
| `en` | English (default and fallback) | English |
| `de` | German | Deutsch |
| `fr` | French | Français |
| `it` | Italian | Italiano |
| `es` | Spanish | Español |

- **Switching:** *Settings → Language*. The selector always shows the native
  language name; the stored value is the language code.
- **Persistence:** a change is written to the settings file immediately
  (Linux: `~/.config/matricula-toolkit/settings.json`, Windows:
  `%APPDATA%\matricula-toolkit\settings.json`, macOS:
  `~/Library/Application Support/matricula-toolkit/settings.json`). If the file
  cannot be written, the language stays active and a warning is shown.
- **First start:** English is used when no language has been stored yet, and
  English is the fallback for any key missing from another locale.
- **Offline:** all translations ship as JSON files
  (`src/matricula_toolkit/desktop_app/locales/<code>.json`); no network access
  is required and no online translation service is used.
- **What is not translated:** OCR/HTR output, transcriptions, corrections,
  user input, database contents, exported files and CLI output. Switching the
  language never alters stored data.
- **Live updates:** static labels, buttons and section titles change
  immediately. Result messages of a running or completed action (for example a
  download message) keep their text until the action is repeated or the section
  is refreshed.
- **Adding a language:** copy `en.json` to `<code>.json`, translate the values
  (keep `{placeholders}` unchanged), and add the code to `SUPPORTED_LANGUAGES`
  and `LANGUAGE_NAMES` in `desktop_app/i18n.py`. The test suite enforces
  complete key sets, identical placeholders, valid format strings and that no
  used value is left in English.

The CLI (`matricula-download`) is intentionally not translated and always
prints English messages.

## OCR and historical handwriting

OCR execution requires the [Kraken](https://github.com/kraken-ocr/kraken) CLI on `PATH`
and a compatible `.mlmodel` model file. Neither is bundled with the toolkit. The
automatic installer (`matricula-install`) can set up Kraken for you.

```bash
# Install Kraken via the installer (recommended)
matricula-install

# Or install Kraken manually
pip install kraken
```

**Kraken models:** The installer does not download models by default because model
choice depends on your source material. Suitable models for historical church books can
be downloaded with `matricula-install` or manually via `kraken get <DOI>`.

Verified models:

| Model | DOI | Type | License | Description |
|-------|-----|------|---------|-------------|
| Kurrent / Sütterlin handwriting HTR | `10.5281/zenodo.21747522` | recognition | CC-BY-4.0 | German Kurrent handwriting, trained on 12 pages (travel diary by Louise Le Beau). Limited training data — usable but not production-grade. Creator: Janosch Alze (hfm Detmold). |
| Fraktur historical print OCR | `10.5281/zenodo.6891852` | recognition | CC-BY-SA-4.0 | 19th century German Fraktur texts, trained on Austrian newspapers. CER: 1.42. Creator: Stefan Weil (Universitätsbibliothek Mannheim). |

**Important:** The Kurrent model is a *handwriting* model (Kurrent/Sütterlin), not a
print model. The Fraktur model is a *print* model for historical German Fraktur type.
Do not confuse the two — using a print model on handwriting (or vice versa) will
produce poor results.

Download a model:

```bash
kraken get 10.5281/zenodo.21747522
kraken get 10.5281/zenodo.6891852
```

Place downloaded `.mlmodel` files in the `models/` directory or any location accessible
to the toolkit.

`prepare_ocr` lists images in the download directory matching the naming pattern
`matricula_<book>_pg<NNN>.jpg`. It is an inventory/preparation step only — no OCR is
executed without Kraken.

OCR jobs run with a configurable timeout (`ocr_timeout_seconds`, default 300s). The
model path is validated before invocation: it must be non-empty, point to an existing
readable file, contain no shell metacharacters, and no path-traversal components (`..`).
Subprocess is invoked in list form (without `shell=True`) to prevent shell injection.

No accuracy guarantees are given. OCR/HTR quality depends on the model, source material,
and image quality.

## Manual transcription correction

The desktop workbench includes a **Transcription correction** section for manually
correcting OCR/HTR transcription errors on church book images. It is the second-to-last
entry in the left-hand navigation panel.

### How it works

1. Open the **Transcription correction** section from the navigation.
2. Select a page/image — the page's PAGE-XML is parsed to extract word positions
   (bounding boxes, text, line numbers). Word overlays are drawn on the image.
3. Click any word on the image (or select it from the word list) to open the
   correction dialog.
4. In the dialog, edit the corrected text in the separate correction field and set
   a readability status (`SICHER`, `UNSICHER`, `UNLESERLICH`). The word's image
   coordinates (X, Y, width, height) are shown for reference.
5. Save — the correction is stored in SQLite with the original text position and
   character offset, so it applies position-accurately even if the corrected word
   has a different length.
6. The corrected transcription is shown below the image and updates automatically
   after each correction.

### Key behaviors

- **Stable positions:** Corrections reference the unchanged original text via
  `original_text_position` and `original_text_length`. Reconstruction applies
  corrections descending by offset, so different word lengths don't shift other
  corrections' positions.
- **Conflict checking:** Overlapping text regions raise a `CorrectionConflictError`.
- **Persistent undo/redo:** Corrections are stored with status `active`, `reverted`,
  or `superseded`, surviving application restarts. Use **Undo** to revert the
  most recent active correction.
- **Protection of manual corrections:** Corrections are linked via `ocr_job_id`;
  new Kraken OCR runs don't overwrite confirmed corrections automatically.
- **PAGE-XML parsing:** Namespace-robust extraction of TextLine/Word/Coords/TextEquiv
  from PAGE-XML files; missing coordinates default to (0, 0, 0, 0).

### Word coordinate requirements

Word overlays require valid bounding-box coordinates from the PAGE-XML:
`x`, `y`, `width`, and `height` must be present and greater than zero. Words
without coordinates are skipped (no overlay is drawn). The coordinates are
absolute pixels in the original image; `image_width_px` and `image_height_px`
store the full image dimensions for durable mapping.

### Missing PAGE-XML data

If no PAGE-XML file is found for an image, the original transcription is empty
and no word overlays are shown. The section still loads, but there are no words
to click or correct. A PAGE-XML file must be generated by Kraken OCR first.

### Limitations

- Only individual **word** corrections are supported in the GUI. The data model
  also allows `region` and `free` correction types, but the GUI does not yet
  provide tools for marking free image areas or regions.
- Corrections are tied to a specific image file (`source_file`) and page number.
  If the image is moved or renamed, the link to its corrections may break.
- The correction dialog requires PySide6 (the `desktop` extra). Without PySide6,
  the desktop workbench starts but the correction section is unavailable.
- Corrections are stored per `source_file` path; there is no cross-image or
  cross-book correction search in this version.

## MCP integration

Run the MCP server (stdio transport):

```bash
python -m matricula_toolkit.mcp_server
```

Available tools:

| Tool | Description |
|------|-------------|
| `inspect_book` | Verify a book is accessible |
| `inspect_page` | Download and inspect a single page |
| `download_page` | Download a single page |
| `download_range` | Download a range of pages |
| `verify_image` | Verify a downloaded image is valid JPEG |
| `diagnose_error` | Diagnose a page download error |
| `prepare_ocr` | List images prepared for OCR; does **not** execute OCR itself |

## Configuration

Copy `.env.example` to `.env` and adjust:

```bash
cp .env.example .env
# Edit .env with your settings
```

All `MATRICULA_*` environment variables are automatically loaded from `.env` via
`python-dotenv` in the CLI, desktop app, and MCP server. Command-line flags override
environment variables.

See `.env.example` for the full list of supported variables, including download
settings (URL, book, output directory, wait time, headless mode, viewport, parallelism,
file size limits) and OCR load protection thresholds (CPU, RAM, temperature, timeout).

No real credentials or private configuration values should be stored in examples.

## Archive providers

The toolkit includes a machine-readable, extensible provider configuration for
external genealogical archives. Provider data is stored in
`src/matricula_toolkit/providers.json` and loaded/validated by
`src/matricula_toolkit/providers.py`. Access control is implemented by
`src/matricula_toolkit/provider_policy.py`.

Provider data is kept strictly separate from the SSRF security logic in
`security.py` and `local_proxy.py`. Both checks must pass for access to be granted;
a provider grant never overrides an SSRF denial.

### Three capabilities (independently checked)

| Capability | Allowed hosts | Additional condition |
|------------|---------------|----------------------|
| Browser navigation | `entry_hosts` only | Authentication must not be `unknown` |
| Proxy connection | `content_hosts` only | None (SSRF still applies) |
| Automated image download | `content_hosts` only | `image_access` must be `permitted_after_review` |

### Current providers

**Enabled (6):**

| ID | Name | Verification | Viewer | Auth | Image access |
|----|------|-------------|--------|------|--------------|
| `matricula-online` | Matricula Online | verified | html_viewer | public | manual_only |
| `digitalarkivet` | Digitalarkivet (NO) | partially_verified | html_viewer | public | manual_only |
| `raduraksti` | Raduraksti (LV) | partially_verified | html_viewer | account_required | manual_only |
| `jewishgen` | JewishGen | partially_verified | index_only | public | unknown |
| `archion` | ARCHION (DE) | partially_verified | unknown | paid_subscription | written_permission_required |
| `familysearch` | FamilySearch | partially_verified | unknown | account_required | manual_only |

**Disabled (4 — unverified):**

| ID | Name | Reason |
|----|------|--------|
| `antenati` | Portale Antenati (IT) | Official site not fully reachable (403); hosts and terms unverified |
| `genbaza` | GenBaza (PL) | Not yet cleared for automated use |
| `arkivalieronline` | Arkivalieronline (DK) | Official site not reachable (transport error) |
| `slave-societies-digital-archive` | Slave Societies Digital Archive | Official site not resolvable; no hosts verified |

### Documentation

See [`docs/PROVIDER_CONFIGURATION.md`](docs/PROVIDER_CONFIGURATION.md) for the full
provider configuration guide, including:

- Adding a new provider
- Verifying hosts and redirects
- Closed vocabularies (provider_type, viewer_type, authentication, image_access,
  verification_status)
- Security rules (no wildcards, no IP literals, no URL credentials, HTTPS + port 443
  only, exact host matching)
- When to enable a provider
- Safe testing

## Local proxy

The toolkit includes a loopback-only asyncio HTTP/CONNECT proxy
(`src/matricula_toolkit/local_proxy.py`) that sits between the downloader and
external archive hosts. It enforces SSRF protection at the network layer:

- Binds to loopback only (127.0.0.1 / ::1)
- Ephemeral port per instance
- `CONNECT_TIMEOUT_SECONDS=30`, `TUNNEL_IO_TIMEOUT_SECONDS=60`
- No open proxy: private targets are rejected
- No environment variable bypass

The proxy works alongside `security.py` (centralized SSRF policy) and
`provider_policy.py` (provider-level access policy). All three must pass for
network access to be granted.

## SSRF protection

Server-Side Request Forgery (SSRF) protection is enforced by
`src/matricula_toolkit/security.py` with a deny-by-default policy:

- **HTTPS only** — no HTTP, no other schemes
- **Port 443 only** — no other ports
- **No URL credentials** — no `user:pass@host`
- **No IP literals** — no IPv4/IPv4-mapped/IPv6 literals
- **DNS rebinding protection** — resolved IPs are re-checked against the allowlist
- **Exact host matching** — subdomains are not implicitly allowed

The provider policy delegates every host check to `security.py`. A provider grant
never overrides an SSRF denial. Both checks must pass.

## Privacy and security

- All processing (downloads, image verification, OCR) runs locally on your machine.
- The toolkit accesses `matricula-online.eu` over HTTPS to download publicly accessible
  church book scans. No other external services are contacted by default.
- Provider-configured archive hosts are accessed only when explicitly enabled and
  when both the provider policy and SSRF policy permit it.
- Kraken model downloads go to the configured Zenodo DOI endpoints.
- No personal genealogical data, GEDCOM files, church book transcriptions, or access
  credentials are stored, transmitted, or processed by the toolkit.
- Source records (download metadata) are stored locally in a SQLite database at the
  platform-appropriate user data directory.
- Model-path validation prevents shell metacharacters and path traversal; subprocess is
  invoked in list form without `shell=True`.
- SSRF policy is deny-by-default: HTTPS only, port 443 only, no credentials, no IP
  literals, DNS rebinding protection.

Avoid absolute statements such as "completely secure" or "guaranteed private." Review
the terms of use of matricula-online.eu and any configured archive provider, and
applicable copyright law in your jurisdiction.

## Development and tests

Install development dependencies:

```bash
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest
```

Run linting:

```bash
ruff check
ruff format --check
```

Run type checking:

```bash
mypy src/matricula_toolkit/
```

Build the wheel:

```bash
python -m build
```

Known typing note: Pillow does not ship `py.typed` markers, so MyPy reports an
`import-untyped` hint for the Pillow import in `downloader.py`. This is pre-existing
and does not affect runtime behaviour.

## License, troubleshooting, and contributing

This tool is licensed under the MIT License. See the [LICENSE](LICENSE) file.

**Legal notice:** This tool downloads publicly accessible scans from Matricula-online
and other configured archive providers. Please respect the terms of use of each
archive and applicable copyright law in your jurisdiction. The authors are not
responsible for misuse of this tool.

**Troubleshooting:**

- If PySide6 is not installed, `matricula-desktop` exits with a clear message.
- If Kraken is not on `PATH`, OCR jobs report that the backend is unavailable.
- If no CPU temperature sensor is found, temperature monitoring reports "not available"
  on Linux.
- The first CPU reading returns "unknown" (baseline); subsequent readings use a
  two-sample delta.
- Disabled providers raise `ProviderPolicyError` on any access attempt — they cannot
  be accidentally used.

**Contributing:** Bug reports and pull requests are welcome. Please open an issue on
GitHub for bugs or feature requests, and submit pull requests against the `main` branch.

When adding a new archive provider, update `src/matricula_toolkit/providers.json`
(never `security.py` or `local_proxy.py` without explicit approval), run the provider
tests, and document the provider in `docs/PROVIDER_CONFIGURATION.md`.

## Help Improve Matricula Toolkit

We welcome contributions from external developers and AI-assisted coding agents alike.
If you would like to help improve this project, here is how to get started:

- **Improvement suggestions and bug fixes are welcome.** Open a GitHub issue to
  discuss a bug or feature idea before implementing it.
- **Existing tests and security mechanisms must be preserved.** Do not weaken or
  remove SSRF protection, provider-policy checks, capability checks, or any existing
  test to make a change easier to implement.
- **New features should be secured with appropriate tests.** Every new capability or
  code path should be accompanied by tests that verify correct behavior and security
  boundaries.
- **Changes should be documented traceably.** Update the README, provider
  documentation, or inline comments so that reviewers can follow your reasoning.
- **External contributions should be submitted as a pull request** against this
  repository. Keep pull requests focused and avoid bundling unrelated changes.
- **AI-assisted coding agents** should follow the repository instructions, respect
  the existing security model, never disclose secrets, and never perform unauthorized
  actions such as force-pushing, merging without review, or accessing external
  systems.

Thank you for helping make the Matricula Toolkit better for everyone.
