# Changelog

## [1.0.0] — 2026-10-31

### Added

- Download retry logic with exponential backoff and configurable `max_retries`,
  `retry_backoff_base`, and `retry_backoff_max` thresholds
- Retry-After header support with configurable maximum backoff cap
- `NetworkError` now carries `status_code` and `retry_after` attributes
- OSError error classification: network-related `errno` values (e.g.
  `ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`) are retried; filesystem errors
  (`EACCES`, `ENOENT`, `ENOSPC`, etc.) are not
- DNS error handling: `EAI_AGAIN` (temporary DNS failure) is retried; all
  other DNS errors (`EAI_FAIL`, `EAI_NONAME`, etc.) are permanent
- Version bumped from 0.4.0 to 1.0.0

### Changed

- Downloader: connection-level errors (e.g. `ConnectionResetError`,
  `ConnectionError`, `asyncio.TimeoutError`) are retried as transient
  network errors
- Downloader: filesystem errors (`PermissionError`, `FileNotFoundError`,
  `OSError` with `ENOSPC`, etc.) are raised immediately without retry
- Downloader: DNS resolution errors (`socket.gaierror` with `EAI_AGAIN`)
  are retried; all other DNS errors are permanent
- `NetworkError` constructor now accepts optional `status_code` and
  `retry_after` parameters

### Notes

- All 418 tests pass (386 existing + 30 new retry tests)
- `ruff check` and `ruff format --check` pass
- `mypy src/` passes with no issues
- Wheel builds successfully (`matricula_toolkit-1.0.0-py3-none-any.whl`)
- Installation in a clean virtual environment verified
- CLI and MCP server smoke-tested successfully

## [0.4.0] — 2026-10-31

### Added

- GUI internationalization for the desktop workbench: English, German, French,
  Italian and Spanish
- `src/matricula_toolkit/desktop_app/i18n.py`: JSON-based `TranslationManager`
  with English fallback, placeholder validation and one-time logging for
  missing keys
- `src/matricula_toolkit/desktop_app/locales/{en,de,fr,it,es}.json`: 372 keys
  per language
- Language selector in *Settings* showing native names (`English`, `Deutsch`,
  `Français`, `Italiano`, `Español`); the selected language code is carried by
  `currentData()`
- Language changes are persisted immediately via
  `MatriculaDesktopService.save_settings()`; write failures show a warning
- `tests/test_i18n.py`: tests for the translation manager, locale
  completeness and quality (key parity, placeholder equality, format strings,
  no English leftovers) and GUI retranslation behaviour (native names,
  persistence, static labels, untouched result texts, modal correction dialog,
  save failure)
- README: "Interface languages" section

### Changed

- All GUI strings in `desktop_app/app.py` go through `t(key, **kwargs)`;
  labels are registered so a language switch updates them without overwriting
  dynamic results
- Fixed placeholder mismatches that dropped values or showed raw
  `{placeholders}` (context panel directory and OCR backend, installer system
  info, temperature, Kraken model label, correction image coordinates)
- Restored the original English wording for labels that had been derived from
  key names (e.g. `Rotate ↺`, `Export sources (CSV)`, `Base URL`, `Busy`)
- `pyproject.toml`: version bumped from 0.3.0 to 0.4.0
- `src/matricula_toolkit/__init__.py`: `__version__` bumped from 0.3.0 to 0.4.0

### Notes

- The CLI is unchanged and stays English
- OCR/HTR results, transcriptions, corrections, user input, database contents
  and exported files are never translated
- The word-correction dialog is modal, so the language cannot change while it
  is open
- Result messages of completed actions update on the next refresh

### Fixed

- The word-correction dialog stored the translated readability label instead of
  the technical status code (`SICHER`/`UNSICHER`/`UNLESERLICH`); stored
  corrections no longer depend on the active UI language
- "Save settings" persisted the native language name instead of the language
  code
- Accessible names (navigation, status bar, image and word lists) now follow a
  language change
- `installer.missing` was left untranslated in German, French, Italian and
  Spanish
- The settings hint showed an outdated Linux settings path
- Two remaining hardcoded labels (image list header, hit-checking image error)
  now use translations
- GUI tests are isolated from the user's real settings file, database and
  language, and the English-leftover check now covers short labels too

## [0.3.0] — 2026-10-30

### Added

- Manual transcription correction mode (`Transkription korrigieren` section in the
  desktop workbench): click OCR words on church book images, correct them in a
  separate dialog, and apply corrections position-accurately to the full
  transcription
- `src/matricula_toolkit/desktop_app/app.py`: `_CorrectionImageLabel` with clickable
  word overlays, `_WordOverlay` clickable word labels, `_CorrectionDialog` for
  editing corrected text and readability status, correction section UI with image
  navigation, word list, undo/refresh controls
- `src/matricula_toolkit/desktop_services.py`: `CorrectionRecord`, `ReadabilityStatus`,
  `CorrectionConflictError`, correction CRUD (`add_correction`, `list_corrections`,
  `update_correction`, `revert_correction`, `restore_correction`, `delete_correction`),
  `build_corrected_transcription`, `parse_kraken_words`, `extract_transcription_from_pagexml`,
  `find_pagexml_for_image`
- `src/matricula_toolkit/source_store.py`: Schema v2 with `corrections` table,
  correction CRUD, conflict detection
- `tests/test_transcription_correction.py`: 12 service-layer tests + 2 GUI tests
  (skipped without `QT_QPA_PLATFORM=offscreen`)
- README.md: manual transcription correction documentation (purpose, GUI section,
  clicking words, correction field, readability, undo/redo, history, PAGE-XML
  requirements, missing PAGE-XML behavior, limitations)
- Version bump 0.2.0 → 0.3.0 (new minor feature)

### Changed

- `pyproject.toml`: version bumped from 0.2.0 to 0.3.0
- `src/matricula_toolkit/__init__.py`: `__version__` bumped from 0.2.0 to 0.3.0

### Notes

- Corrections are stored in SQLite with status `active`, `reverted`, or `superseded`
  for persistent undo/redo across application restarts
- Corrections are linked via `ocr_job_id`; new Kraken OCR runs don't overwrite
  confirmed corrections automatically
- Only `word` correction type is exposed in the GUI; `region` and `free` types exist
  in the data model but have no GUI tools yet
- Requires PySide6 (`desktop` extra); the desktop workbench starts without it but
  the correction section is unavailable

## [0.2.0] — 2026-10-29

### Added

- Extensible, machine-readable provider configuration for external genealogical
  archives (`src/matricula_toolkit/providers.json` with 10 providers)
- `src/matricula_toolkit/providers.py`: Provider dataclass, loading, validation,
  duplicate ID detection, closed-vocabulary field validation
- `src/matricula_toolkit/provider_policy.py`: ProviderPolicy with three capabilities
  (browser navigation, proxy connection, automated image download), SSRF delegation,
  redirect validation, and combined access checks
- `docs/PROVIDER_CONFIGURATION.md`: full provider configuration guide
- `tests/test_providers.py`: 22 tests (loading, validation, duplicates, missing fields,
  host matching)
- `tests/test_provider_policy.py`: 22 tests (capability checks, access classes, redirect
  validation, SSRF preservation)
- README.md: archive providers section, local proxy documentation, SSRF protection
  explanation, architecture/dataflow overview

### Changed

- `pyproject.toml`: version bumped from 0.1.2 to 0.2.0; description expanded to mention
  provider configuration
- `src/matricula_toolkit/__init__.py`: `__version__` bumped from 0.1.2 to 0.2.0
- `README.md`: fundamentally updated with project description, architecture/dataflow,
  local proxy, SSRF protection, archive providers, installation/usage, security notices

### Notes

- Provider data is strictly separated from SSRF security logic (`security.py`,
  `local_proxy.py`); both checks must pass, provider grant never overrides SSRF denial
- Providers are disabled by default; unknown hosts/viewer/authentication values never
  auto-enabled
- No wildcards, no IP literals, no URL credentials; HTTPS + port 443 only
- No existing files were modified for the provider configuration itself (6 new files
  added, 0 existing files changed)
- SSRF security logic unchanged — no modifications to `security.py` or `local_proxy.py`

## [0.1.2] — 2026-10-28

### Added

- Automatic installation assistant (`matricula-install`) with OS detection,
  dependency checks, venv setup, Kraken OCR backend, and model management
- `src/matricula_toolkit/installer.py`: system detection (OS, distribution,
  architecture, Python, RAM, disk, GPU, container), component detection
  (Python, pip, venv, Playwright, Pillow, python-dotenv, PySide6, MCP,
  Kraken CLI, kraken-python, Chromium), install plan computation, venv
  creation, system package installation (apt/dnf/yum/pacman/zypper/brew/
  choco/winget), Python package installation, Playwright browser install,
  Kraken model download via `kraken get`, installation verification
- `src/matricula_toolkit/installer_cli.py`: CLI entry point with
  `--check-only`, `--no-venv`, `--venv-path`, `--no-kraken`, `--no-models`,
  `--non-interactive` flags
- Verified Kraken models table (`KRAKEN_MODELS`) with DOI, license, creator,
  and description for:
  - Kurrent/Sütterlin handwriting HTR model (10.5281/zenodo.21747522,
    CC-BY-4.0, Janosch Alze)
  - Fraktur historical print OCR model (10.5281/zenodo.6891852,
    CC-BY-SA-4.0, Stefan Weil)
- 23 installer tests (mock-based, no system changes)

### Changed

- `pyproject.toml`: added `matricula-install` entry point
- `README.md`: added Automatic Installer section and verified Kraken model
  documentation

### Notes

- No silent `sudo`: system packages require interactive confirmation or are
  skipped in non-interactive mode
- Idempotent: re-running does not damage a working installation
- All processing local; no cloud services or paid APIs
- Kraken model download is optional (`--no-models` to skip)

## [0.1.1] — 2026-10-27 (QA-Nachbesserung)

### Added

- CLI with `page`, `range`, `verify`, `diagnose` commands
- MCP server with 7 tools: `inspect_book`, `inspect_page`, `download_page`,
  `download_range`, `verify_image`, `diagnose_error`, `prepare_ocr`
- Playwright/Chromium-based downloader that intercepts high-resolution
  image URLs from Matricula network requests
- Resume support: skips already-downloaded files
- JPEG verification (magic bytes check)
- Configuration via environment variables and `.env` file
- Comprehensive error handling (403, 404, 429, ImageNotFoundError, BrowserError)
- 45 unit and integration tests with mocked Playwright
- MIT License, README, MCP configuration docs, legal notice

### Method

- Opens Matricula viewer URL (`?pg=N`) with Playwright/Chromium
- Waits configurable seconds per page (default: 5s)
- Intercepts image URL from network requests to `img.data.matricula-online.eu`
- Downloads the high-resolution image (not thumbnails)
- Saves as `matricula_<book>_pg<NNN>.jpg`

## [0.1.1] — 2026-10-27 (QA-Nachbesserung)

### Added

- Persistent SQLite source storage (`source_store.py`) with WAL mode,
  busy-timeout, schema versioning, CRUD, JSON/CSV export
- Platform-appropriate default database path (XDG_DATA_HOME / APPDATA /
  ~/Library/Application Support)
- CPU delta measurement from `/proc/stat` (first call returns `None`/unknown)
- CPU temperature reading from `/sys/class/thermal` with CPU-sensor
  identification (`x86_pkg_temp`, `coretemp`, `k10temp`, `zenpower`,
  `acpitz`, `cpu`, `processor`) and plausibility check (0–200 °C)
- OCR load protection: blocks OCR jobs when CPU/RAM/temperature exceed
  configurable thresholds; unknown sensor values block when limit is set
- OCR job hardening: timeout, model-path validation (no shell metachars,
  no `..`, file must exist), list-form `subprocess.run` (no `shell=True`),
  `_active_ocr_jobs` counter reset in `finally`
- `.env` loading via `python-dotenv` in CLI, desktop app, and MCP server
- `PySide6` moved to optional `desktop` extra; CLI works without it
- 113 tests total (72 existing + 41 new QA improvement tests)

### Changed

- `pyproject.toml`: `python-dotenv` as core dependency; `PySide6` in
  optional `desktop` extra
- `cli.py`, `desktop_app/app.py`, `mcp_server.py`: `load_dotenv()` in `main()`
- `desktop_services.py`: `SourceStore` always initialized with
  `default_db_path()`; CPU first-call returns `None`; temperature returns
  `None` when no sensor found
- `downloader.py`: JPEG validation via full PIL decode (not just magic
  bytes), atomic save via `tempfile.mkstemp` + `os.replace`

### Fixed

- `test_desktop_app.py`: `cpu_percent` can be `None`; `ocr_max_temperature_c=None`
  for missing sensor; DB isolation fixture per test
- `test_qa_improvements.py`: new file with 654 lines covering SQLite path,
  CRUD, CPU delta, temperature, OCR load protection, `.env` loading

### Method

- Unchanged from 0.1.0

### Legal Notice

- Unchanged from 0.1.0