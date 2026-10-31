"""Translation manager for the desktop workbench GUI.

Loads JSON-based locale files and provides a ``t(key, **kwargs)`` lookup with
English fallback and placeholder formatting.  Missing keys are logged once per
key per language via Python's standard ``logging`` module and fall back to the
key itself (never a crash).

The CLI is intentionally *not* translated — this module is only imported by
``desktop_app/app.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default / fallback language (English is the reference translation).
DEFAULT_LANGUAGE = "en"

#: All supported language codes.
SUPPORTED_LANGUAGES = ("en", "de", "fr", "it", "es")

#: Native display names for the language selector (never translated).
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "es": "Español",
}

#: Directory containing ``<lang>.json`` locale files, next to this module.
_LOCALES_DIR = Path(__file__).parent / "locales"

#: Keys that have already been warned about, per language, to avoid duplicate
#: log spam for the same missing key.
_warned_keys: dict[str, set[str]] = {lang: set() for lang in SUPPORTED_LANGUAGES}


class TranslationManager:
    """Loads and provides translations for the GUI.

    Lookup order for a key in language ``L``:

    1. ``L`` translation
    2. English translation (reference fallback)
    3. The key itself (last resort, never a crash)

    Placeholders are substituted with :meth:`str.format` using keyword
    arguments passed to :meth:`t`.  A missing or misspelled placeholder leaves
    the template unchanged and logs a warning.
    """

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self._translations: dict[str, dict[str, str]] = {}
        self._language = DEFAULT_LANGUAGE
        self._load_all()
        self.set_language(language)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _load_all(self) -> None:
        """Load every supported locale file; missing/invalid files are skipped."""
        for lang in SUPPORTED_LANGUAGES:
            self._load_language(lang)

    def _load_language(self, lang: str) -> None:
        """Load a single locale file into ``_translations[lang]``."""
        path = _LOCALES_DIR / f"{lang}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("Locale file not found: %s", path)
            self._translations[lang] = {}
            return
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in locale file %s: %s", path, exc)
            self._translations[lang] = {}
            return
        except OSError as exc:
            logger.warning("Could not read locale file %s: %s", path, exc)
            self._translations[lang] = {}
            return

        if not isinstance(data, dict):
            logger.warning("Locale file %s does not contain a JSON object", path)
            self._translations[lang] = {}
            return

        # Flatten: all values must be strings.
        self._translations[lang] = {
            str(k): str(v) for k, v in data.items() if isinstance(v, str)
        }

    # ------------------------------------------------------------------
    # Language management
    # ------------------------------------------------------------------
    def set_language(self, language: str) -> None:
        """Set the active UI language.

        Falls back to English if the requested language is unsupported or its
        locale file could not be loaded.
        """
        if language not in SUPPORTED_LANGUAGES:
            logger.warning(
                "Unsupported language %r, falling back to %r",
                language,
                DEFAULT_LANGUAGE,
            )
            language = DEFAULT_LANGUAGE
        if language not in self._translations or not self._translations[language]:
            logger.warning(
                "No translations loaded for %r, falling back to %r",
                language,
                DEFAULT_LANGUAGE,
            )
            language = DEFAULT_LANGUAGE
        self._language = language

    def get_language(self) -> str:
        """Return the currently active language code."""
        return self._language

    def get_available_languages(self) -> list[str]:
        """Return all supported language codes."""
        return list(SUPPORTED_LANGUAGES)

    # ------------------------------------------------------------------
    # Translation lookup
    # ------------------------------------------------------------------
    def t(self, key: str, **kwargs: Any) -> str:
        """Translate ``key`` into the active language.

        Lookup order: active language → English → key itself.
        Placeholders (e.g. ``{page_number}``) are substituted from ``kwargs``.
        Missing keys are logged once per key per language.
        """
        text = self._lookup(key)
        if text is None:
            self._log_missing(key)
            text = key
        if kwargs:
            text = self._format(text, key, kwargs)
        return text

    def _lookup(self, key: str) -> str | None:
        """Look up ``key`` in active language, then English."""
        translations = self._translations.get(self._language, {})
        if key in translations:
            return translations[key]
        # English fallback
        en = self._translations.get(DEFAULT_LANGUAGE, {})
        if key in en:
            return en[key]
        return None

    def _log_missing(self, key: str) -> None:
        """Log a missing-key warning once per key per language."""
        lang = self._language
        if key in _warned_keys[lang]:
            return
        _warned_keys[lang].add(key)
        logger.warning("Missing translation key %r in language %r", key, lang)

    @staticmethod
    def _format(text: str, key: str, kwargs: dict[str, Any]) -> str:
        """Substitute placeholders; on error return the unformatted text."""
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("Placeholder error for key %r: %s (text=%r)", key, exc, text)
            return text


#: Module-level singleton used by ``desktop_app/app.py``.
translator = TranslationManager()


def t(key: str, **kwargs: Any) -> str:
    """Module-level convenience wrapper around :meth:`TranslationManager.t`."""
    return translator.t(key, **kwargs)


def set_language(language: str) -> None:
    """Module-level convenience wrapper around :meth:`TranslationManager.set_language`."""
    translator.set_language(language)


def get_language() -> str:
    """Module-level convenience wrapper around :meth:`TranslationManager.get_language`."""
    return translator.get_language()


def get_available_languages() -> list[str]:
    """Module-level convenience wrapper around :meth:`TranslationManager.get_available_languages`."""
    return translator.get_available_languages()


def get_language_name(language: str) -> str:
    """Return the native display name for ``language`` (falls back to the code)."""
    return LANGUAGE_NAMES.get(language, language)
