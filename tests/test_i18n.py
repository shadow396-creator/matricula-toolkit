"""Unit tests for the GUI internationalization (i18n) module."""

import ast
import json
import re
from pathlib import Path

import pytest

from matricula_toolkit.desktop_app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_available_languages,
    get_language,
    set_language,
    t,
)
from matricula_toolkit.desktop_services import MatriculaDesktopService


@pytest.fixture(autouse=True)
def _isolate_desktop_state(tmp_path, monkeypatch):
    """Keep tests away from the user's real settings file, database and language.

    Without this the GUI tests would read ``~/.config/matricula-toolkit/settings.json``
    (or the platform equivalent), so their result would depend on the developer's
    own language choice, and a language switch could overwrite real settings.
    """
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "matricula_toolkit.desktop_services.default_db_path",
        lambda: tmp_path / "sources.db",
    )
    set_language(DEFAULT_LANGUAGE)
    yield
    set_language(DEFAULT_LANGUAGE)


# ------------------------------------------------------------------
# TranslationManager unit tests
# ------------------------------------------------------------------


def test_default_language_is_english():
    assert DEFAULT_LANGUAGE == "en"


def test_supported_languages_include_expected():
    assert set(SUPPORTED_LANGUAGES) == {"en", "de", "fr", "it", "es"}


def test_get_available_languages_returns_all_supported():
    langs = get_available_languages()
    assert set(langs) == set(SUPPORTED_LANGUAGES)


def test_get_language_defaults_to_english():
    # translator is a module-level singleton; reset to English
    set_language("en")
    assert get_language() == "en"


def test_set_and_get_language():
    set_language("de")
    assert get_language() == "de"
    set_language("en")
    assert get_language() == "en"


def test_set_unsupported_language_falls_back_to_english(caplog):
    set_language("xx")
    assert get_language() == "en"
    assert "Unsupported language" in caplog.text


def test_t_returns_english_by_default():
    set_language("en")
    assert t("window.title") == "Matricula Toolkit — Workbench"


def test_t_returns_translated_text():
    set_language("de")
    title = t("window.title")
    assert title != "Matricula Toolkit — Workbench"
    assert title == "Matricula Toolkit — Arbeitsfläche"


def test_t_falls_back_to_english_for_missing_key(caplog):
    set_language("de")
    # A key that exists in en but not in de should fall back to en
    result = t("nonexistent.key.xyz")
    assert result == "nonexistent.key.xyz"
    assert "Missing translation key" in caplog.text


def test_t_placeholder_formatting():
    set_language("en")
    result = t("context.book", book="01519")
    assert "01519" in result


def test_t_placeholder_formatting_in_german():
    set_language("de")
    result = t("context.book", book="01519")
    assert "01519" in result


def test_t_missing_placeholder_logs_warning(caplog):
    set_language("en")
    # system.cpu uses {percent}; passing wrong kwarg should warn
    result = t("system.cpu", cpu=50)
    assert "CPU: {percent} %" in result  # unformatted, placeholder left as-is
    assert "Placeholder error" in caplog.text


def test_locale_files_have_same_key_count():
    """All locale files must have the same number of keys as en.json."""
    locales_dir = (
        Path(__file__).parent.parent
        / "src"
        / "matricula_toolkit"
        / "desktop_app"
        / "locales"
    )
    en_path = locales_dir / "en.json"
    with open(en_path, encoding="utf-8") as f:
        en_keys = set(json.load(f).keys())
    for lang in SUPPORTED_LANGUAGES:
        path = locales_dir / f"{lang}.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert set(data.keys()) == en_keys, f"{lang}.json key set differs from en.json"


def test_locale_files_are_valid_json():
    """All locale files must be valid JSON objects."""
    locales_dir = (
        Path(__file__).parent.parent
        / "src"
        / "matricula_toolkit"
        / "desktop_app"
        / "locales"
    )
    for lang in SUPPORTED_LANGUAGES:
        path = locales_dir / f"{lang}.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)
        for k, v in data.items():
            assert isinstance(k, str)
            assert isinstance(v, str)


# ------------------------------------------------------------------
# Locale completeness and quality checks
# ------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_LOCALES_DIR = _PROJECT_ROOT / "src" / "matricula_toolkit" / "desktop_app" / "locales"
_APP_PY = _PROJECT_ROOT / "src" / "matricula_toolkit" / "desktop_app" / "app.py"

#: Keys whose value is legitimately identical to English in *every* language
#: (separator pseudo-key, brand text and technical value templates).
_IDENTICAL_TO_ENGLISH_ALL = frozenset(
    {
        " | ",
        "context.version",
        "dashboard.cpu",
        "installer.kraken_model",
        "installer.ok",
        "sources.record",
    }
)

#: ``(key, language)`` pairs where the word is spelled identically in that
#: language, so the value must not be reported as an untranslated leftover.
_IDENTICAL_TO_ENGLISH_BY_LANG = frozenset(
    {
        ("books.image_label", "de"),
        ("books.image_label", "es"),
        ("books.image_label", "it"),
        ("common.error_title", "es"),
        ("dashboard.downloads", "de"),
        ("dashboard.images", "fr"),
        ("image_browser.file", "it"),
        ("image_browser.images", "fr"),
        ("image_browser.page_info", "fr"),
        ("installer.error_prefix", "es"),
        ("installer.optional", "de"),
        ("section.dashboard", "de"),
        ("section.dashboard", "it"),
        ("section.installer", "de"),
        ("section.installer", "it"),
        ("section.sources", "fr"),
        ("section.system", "de"),
        ("sources.notes", "fr"),
        ("sources.status", "de"),
        ("system.cpu", "de"),
        ("system.cpu", "es"),
        ("system.cpu", "it"),
        ("system.ram", "de"),
        ("system.ram", "es"),
        ("system.ram", "it"),
        ("transcription_correction.error_title", "es"),
        ("transcription_correction.page_info", "fr"),
        ("transcription_correction.position", "de"),
        ("transcription_correction.transcription", "fr"),
    }
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")


def _load_all_locales() -> dict[str, dict[str, str]]:
    """Load every locale file into a ``{language: {key: text}}`` mapping."""
    return {
        lang: json.loads((_LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        for lang in SUPPORTED_LANGUAGES
    }


def _section_keys_from_app_source() -> set[str]:
    """Return the section translation keys referenced by ``_SECTION_T_KEYS``."""
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "_SECTION_T_KEYS"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Dict):
            keys |= {
                value.value
                for value in node.value.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
    return keys


def _translation_keys_used_in_app() -> set[str]:
    """Return every translation key that ``app.py`` can look up at runtime."""
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "t"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    # Keys composed at runtime: f"dashboard.{label}" and _SECTION_T_KEYS lookups.
    keys |= {f"dashboard.{name}" for name in ("downloads", "images", "ocr_jobs", "cpu")}
    keys |= _section_keys_from_app_source()
    return keys


def test_all_translation_keys_used_in_app_exist_in_every_language():
    """Every key the GUI can look up must be defined in all five locale files."""
    used = _translation_keys_used_in_app()
    assert used, "no translation keys found in app.py"
    for lang, translations in _load_all_locales().items():
        missing = sorted(used - set(translations))
        assert not missing, f"{lang}.json is missing keys used by the GUI: {missing}"


def test_no_translation_value_is_empty():
    """No locale file may contain an empty translation value."""
    for lang, translations in _load_all_locales().items():
        empty = sorted(key for key, value in translations.items() if not value.strip())
        assert not empty, f"{lang}.json has empty values: {empty}"


def test_placeholders_match_across_languages():
    """Every language must use exactly the same placeholders as English."""
    locales = _load_all_locales()
    english = locales[DEFAULT_LANGUAGE]
    for lang, translations in locales.items():
        if lang == DEFAULT_LANGUAGE:
            continue
        for key, text in english.items():
            expected = set(_PLACEHOLDER_RE.findall(text))
            actual = set(_PLACEHOLDER_RE.findall(translations[key]))
            assert actual == expected, (
                f"{lang}.json placeholder mismatch for {key!r}: {actual} != {expected}"
            )


def test_translation_format_strings_are_valid():
    """Every translation must survive ``str.format`` with its own placeholders."""

    class _Value:
        def __format__(self, spec: str) -> str:
            return "0"

    for lang, translations in _load_all_locales().items():
        for key, text in translations.items():
            names = _PLACEHOLDER_RE.findall(text)
            try:
                text.format(**{name: _Value() for name in names})
            except (KeyError, IndexError, ValueError) as exc:
                raise AssertionError(
                    f"{lang}.json invalid format string for {key!r}: {exc}"
                ) from exc


def test_translations_are_not_left_in_english():
    """Every used key must differ from the English reference in each language.

    Short labels are checked too, so a single untranslated word such as
    ``installer.missing`` cannot slip through; values that are spelled
    identically in the target language are listed explicitly.
    """
    locales = _load_all_locales()
    english = locales[DEFAULT_LANGUAGE]
    used = _translation_keys_used_in_app()
    leftovers = sorted(
        f"{lang}:{key}"
        for lang, translations in locales.items()
        if lang != DEFAULT_LANGUAGE
        for key in used
        if key not in _IDENTICAL_TO_ENGLISH_ALL
        and (key, lang) not in _IDENTICAL_TO_ENGLISH_BY_LANG
        and translations[key] == english[key]
    )
    assert not leftovers, f"locale values still identical to English: {leftovers}"


def test_english_reference_contains_no_german_text():
    """The English reference must not contain German umlauts."""
    english = _load_all_locales()[DEFAULT_LANGUAGE]
    suspicious = sorted(
        key for key, value in english.items() if any(ch in value for ch in "äöüßÄÖÜ")
    )
    assert not suspicious, f"en.json contains German text: {suspicious}"


def test_missing_key_logged_once_per_language(caplog):
    """Missing keys should only be logged once per key per language."""
    set_language("de")
    caplog.clear()
    t("missing.key.test")
    t("missing.key.test")
    assert caplog.text.count("Missing translation key") == 1


# ------------------------------------------------------------------
# Desktop services language persistence tests
# ------------------------------------------------------------------


def test_desktop_service_has_language_field(tmp_path):
    service = MatriculaDesktopService(
        book="01519",
        out_dir=str(tmp_path / "downloads"),
        settings_file=str(tmp_path / "settings.json"),
    )
    assert service.language == "en"


def test_desktop_service_saves_and_loads_language(tmp_path):
    settings_file = tmp_path / "settings.json"
    service = MatriculaDesktopService(
        book="01519",
        out_dir=str(tmp_path / "downloads"),
        settings_file=str(settings_file),
    )
    service.language = "de"
    service.save_settings()

    # Load into a new service instance
    service2 = MatriculaDesktopService(
        book="01519",
        out_dir=str(tmp_path / "downloads"),
        settings_file=str(settings_file),
    )
    service2._load_settings()
    assert service2.language == "de"


# ------------------------------------------------------------------
# GUI language switch tests (require offscreen Qt)
# ------------------------------------------------------------------


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_switch_retranslates():
    """Verify that changing the language re-translates the GUI."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    window = MainWindow()

    # Window title should be English by default
    assert window.windowTitle() == "Matricula Toolkit — Workbench"

    # Switch to German
    set_language("de")
    window._retranslate_ui()
    assert window.windowTitle() == "Matricula Toolkit — Arbeitsfläche"

    # Switch back to English
    set_language("en")
    window._retranslate_ui()
    assert window.windowTitle() == "Matricula Toolkit — Workbench"

    window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_navigation_items_translated():
    """Verify navigation sidebar items are translated."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    window = MainWindow()

    # English
    set_language("en")
    window._retranslate_ui()
    item0 = window.navigation.item(0).text()
    assert item0 == "Dashboard"

    # German
    set_language("de")
    window._retranslate_ui()
    item0 = window.navigation.item(0).text()
    assert item0 == "Dashboard"  # section.dashboard in German

    window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_combo_uses_native_names(tmp_path):
    """The selector shows native names while currentData() carries the code."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    set_language("en")
    window = MainWindow()
    window.service.settings_file = str(tmp_path / "settings.json")
    try:
        combo = window._lang_combo
        names = [combo.itemText(i) for i in range(combo.count())]
        codes = [combo.itemData(i) for i in range(combo.count())]
        assert names == ["English", "Deutsch", "Français", "Italiano", "Español"]
        assert codes == list(SUPPORTED_LANGUAGES)
        assert combo.currentData() == "en"

        combo.setCurrentIndex(combo.findData("de"))
        assert get_language() == "de"
        assert combo.currentData() == "de"
        assert combo.currentText() == "Deutsch"
    finally:
        set_language("en")
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_change_persists_immediately(tmp_path):
    """Switching the language writes the new value to the settings file."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    set_language("en")
    settings_file = tmp_path / "settings.json"
    window = MainWindow()
    window.service.settings_file = str(settings_file)
    try:
        window._lang_combo.setCurrentIndex(window._lang_combo.findData("fr"))

        assert window.service.language == "fr"
        assert settings_file.is_file()
        stored = json.loads(settings_file.read_text(encoding="utf-8"))
        assert stored["language"] == "fr"

        # A fresh service instance reads the persisted language back.
        service = MatriculaDesktopService(
            base_url="",
            book="01519",
            out_dir=str(tmp_path / "downloads"),
            settings_file=str(settings_file),
        )
        service._load_settings()
        assert service.language == "fr"
    finally:
        set_language("en")
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_switch_retranslates_static_labels(tmp_path):
    """Static labels registered at build time follow a language change."""
    from PySide6.QtWidgets import QApplication, QLabel

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    set_language("en")
    window = MainWindow()
    window.service.settings_file = str(tmp_path / "settings.json")
    try:
        window.service.language = "en"
        set_language("en")
        window._retranslate_ui()
        english = {label.text() for label in window.findChildren(QLabel)}
        assert "Component status" in english
        assert "Kraken models (optional)" in english
        assert "Installation log" in english

        set_language("de")
        window._retranslate_ui()
        german = {label.text() for label in window.findChildren(QLabel)}
        assert "Komponentenstatus" in german
        assert "Kraken-Modelle (optional)" in german
        assert "Installationsprotokoll" in german
        assert "Component status" not in german
    finally:
        set_language("en")
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_switch_keeps_dynamic_result_text(tmp_path):
    """A result text produced by an action is never overwritten on switch."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    set_language("en")
    window = MainWindow()
    window.service.settings_file = str(tmp_path / "settings.json")
    try:
        window.service.language = "en"
        set_language("en")
        window._retranslate_ui()
        window._books_result.setText("CUSTOM RESULT 42")

        set_language("de")
        window._retranslate_ui()

        assert window._books_result.text() == "CUSTOM RESULT 42"
    finally:
        set_language("en")
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_correction_dialog_is_modal():
    """The word-correction dialog is modal, so no language switch can happen inside it."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import _CorrectionDialog

    QApplication.instance() or QApplication([])
    service = MatriculaDesktopService(base_url="", book="01519", out_dir="./downloads")
    dialog = _CorrectionDialog(
        {"text": "wort", "x": 1, "y": 2, "width": 3, "height": 4},
        "page.jpg",
        1,
        service,
    )
    try:
        assert dialog.isModal()
    finally:
        dialog.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_language_save_failure_is_reported(tmp_path, monkeypatch):
    """A failed settings write keeps the language active and shows a warning."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    set_language("en")
    window = MainWindow()
    window.service.settings_file = str(tmp_path / "settings.json")
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args)
    )

    def _fail() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(window.service, "save_settings", _fail)
    try:
        window._lang_combo.setCurrentIndex(window._lang_combo.findData("it"))

        assert get_language() == "it"
        assert warnings, "expected a user-visible warning"
    finally:
        set_language("en")
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_correction_dialog_readability_uses_codes(tmp_path):
    """The readability combo shows translated labels but carries status codes."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import _CorrectionDialog
    from matricula_toolkit.desktop_services import ReadabilityStatus

    QApplication.instance() or QApplication([])
    service = MatriculaDesktopService(
        base_url="",
        book="01519",
        out_dir=str(tmp_path / "downloads"),
        settings_file=str(tmp_path / "settings.json"),
        db_path=str(tmp_path / "sources.db"),
    )
    word = {
        "text": "Johann",
        "x": 1,
        "y": 2,
        "width": 3,
        "height": 4,
        "position": 0,
        "line": 1,
    }
    expected_codes = [
        ReadabilityStatus.SICHER,
        ReadabilityStatus.UNSICHER,
        ReadabilityStatus.UNLESERLICH,
    ]
    labels: dict[str, list[str]] = {}
    for language in ("en", "de", "fr"):
        set_language(language)
        dialog = _CorrectionDialog(word, "page.jpg", 1, service)
        try:
            codes = [dialog._readability.itemData(i) for i in range(3)]
            assert codes == expected_codes
            labels[language] = [dialog._readability.itemText(i) for i in range(3)]
        finally:
            dialog.close()
    assert labels["de"] != labels["en"]
    assert labels["fr"] != labels["en"]


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_correction_dialog_saves_readability_code(tmp_path):
    """Saving in a non-English UI stores the status code, never the label."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import _CorrectionDialog
    from matricula_toolkit.desktop_services import ReadabilityStatus

    QApplication.instance() or QApplication([])
    service = MatriculaDesktopService(
        base_url="",
        book="01519",
        out_dir=str(tmp_path / "downloads"),
        settings_file=str(tmp_path / "settings.json"),
        db_path=str(tmp_path / "sources.db"),
    )
    set_language("de")
    word = {
        "text": "Johann",
        "x": 1,
        "y": 2,
        "width": 3,
        "height": 4,
        "position": 0,
        "line": 1,
    }
    dialog = _CorrectionDialog(word, "page.jpg", 1, service)
    try:
        assert dialog._readability.currentText() == "Sicher"
        dialog._readability.setCurrentIndex(1)  # "Unsicher"
        dialog._corr_edit.setPlainText("Johannes")
        dialog._save()
    finally:
        dialog.close()

    corrections = service.list_corrections(status="active")
    assert len(corrections) == 1
    assert corrections[0].readability == ReadabilityStatus.UNSICHER
    assert corrections[0].readability != "Unsicher"


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_settings_save_persists_language_code(tmp_path):
    """'Save settings' stores the language code, not the native display name."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    settings_file = tmp_path / "settings.json"
    window = MainWindow()
    window.service.settings_file = str(settings_file)
    window._set_settings_file.setText(str(settings_file))
    try:
        window._lang_combo.setCurrentIndex(window._lang_combo.findData("de"))
        window._on_settings_save()

        stored = json.loads(settings_file.read_text(encoding="utf-8"))
        assert stored["language"] == "de"
        assert window.service.language == "de"
    finally:
        window.close()


@pytest.mark.skipif(
    "QT_QPA_PLATFORM" not in __import__("os").environ
    or __import__("os").environ["QT_QPA_PLATFORM"] != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_gui_invalid_persisted_language_falls_back_to_english(tmp_path):
    """An unsupported persisted language code is normalised to English."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    payload = json.dumps({"language": "xx"})
    for base in (tmp_path / "config", tmp_path):
        directory = base / "matricula-toolkit"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(payload, encoding="utf-8")

    QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        assert window.service.language == "en"
        assert window._lang_combo.currentData() == "en"
        assert window.windowTitle() == "Matricula Toolkit — Workbench"
    finally:
        window.close()
