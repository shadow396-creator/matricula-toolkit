"""PySide6 workbench for the Matricula Toolkit.

Provides a dark-themed desktop workbench with navigation, status bar, and
section pages for dashboard, research, book/image browsing, hit checking,
source management, OCR jobs, system monitoring, settings, and help.

All long-running operations (downloads, OCR) run in worker threads so the
GUI stays responsive. The service layer bridges to the public toolkit
backend (downloader, config) without duplicating business logic.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from matricula_toolkit import __version__
from matricula_toolkit.desktop_app.i18n import (
    DEFAULT_LANGUAGE,
    get_available_languages,
    get_language_name,
    t,
    translator,
)
from matricula_toolkit.desktop_services import (
    MatriculaDesktopService,
    ReadabilityStatus,
)

try:
    from matricula_toolkit.installer import (
        DEFAULT_KRAKEN_MODELS,
        KRAKEN_MODELS,
        Installer,
        InstallPlan,
        InstallResult,
        compute_install_plan,
        detect_components,
        detect_system,
    )
except ImportError:  # pragma: no cover - installer module always present
    DEFAULT_KRAKEN_MODELS: list[str] = []  # type: ignore
    KRAKEN_MODELS: dict[str, dict[str, str]] = {}  # type: ignore

    def detect_system() -> Any:  # type: ignore[misc]
        return None

    def detect_components() -> list[Any]:  # type: ignore[misc]
        return []

    def compute_install_plan(  # type: ignore[misc]
        *_args: Any, **_kwargs: Any
    ) -> Any:
        return None

    InstallPlan = None  # type: ignore
    InstallResult = None  # type: ignore
    Installer = None  # type: ignore

try:
    from PySide6.QtCore import QObject, QPoint, QSize, Qt, QThread, Signal
    from PySide6.QtGui import QPixmap, QTransform
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSplitter,
        QStackedWidget,
        QTextBrowser,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - depends on the environment
    QT_AVAILABLE = False
    QT_IMPORT_ERROR: ImportError | None = exc
    Qt: Any | None = None  # type: ignore[no-redef]
    QApplication: Any | None = None  # type: ignore[no-redef]
    QDialog: Any | None = None  # type: ignore[no-redef]
    QDialogButtonBox: Any | None = None  # type: ignore[no-redef]
    QFrame: Any | None = None  # type: ignore[no-redef]
    QGridLayout: Any | None = None  # type: ignore[no-redef]
    QHBoxLayout: Any | None = None  # type: ignore[no-redef]
    QLabel: Any | None = None  # type: ignore[no-redef]
    QLineEdit: Any | None = None  # type: ignore[no-redef]
    QListWidget: Any | None = None  # type: ignore[no-redef]
    QListWidgetItem: Any | None = None  # type: ignore[no-redef]
    QMainWindow: Any | None = None  # type: ignore[no-redef]
    QMessageBox: Any | None = None  # type: ignore[no-redef]
    QPlainTextEdit: Any | None = None  # type: ignore[no-redef]
    QProgressBar: Any | None = None  # type: ignore[no-redef]
    QPushButton: Any | None = None  # type: ignore[no-redef]
    QScrollArea: Any | None = None  # type: ignore[no-redef]
    QSplitter: Any | None = None  # type: ignore[no-redef]
    QStackedWidget: Any | None = None  # type: ignore[no-redef]
    QTextEdit: Any | None = None  # type: ignore[no-redef]
    QVBoxLayout: Any | None = None  # type: ignore[no-redef]
    QWidget: Any | None = None  # type: ignore[no-redef]
    QObject: Any | None = None  # type: ignore[no-redef]
    QPoint: Any | None = None  # type: ignore[no-redef]
    QSize: Any | None = None  # type: ignore[no-redef]
    QThread: Any | None = None  # type: ignore[no-redef]
    Signal: Any | None = None  # type: ignore[no-redef]
    QPixmap: Any | None = None  # type: ignore[no-redef]
    QTransform: Any | None = None  # type: ignore[no-redef]
else:
    QT_AVAILABLE = True
    QT_IMPORT_ERROR = None

#: Internal section keys (English, used for lookups). Display names are
#: translated via ``t("section.<key>")`` in the navigation sidebar.
SECTIONS = (
    "Dashboard",
    "Research",
    "Books",
    "Image browser",
    "Hit checking",
    "Sources",
    "OCR jobs",
    "System",
    "Installer",
    "Settings",
    "Transkription korrigieren",
    "Help",
)

#: Mapping from internal section key to translation key.
_SECTION_T_KEYS = {
    "Dashboard": "section.dashboard",
    "Research": "section.research",
    "Books": "section.books",
    "Image browser": "section.image_browser",
    "Hit checking": "section.hit_checking",
    "Sources": "section.sources",
    "OCR jobs": "section.ocr_jobs",
    "System": "section.system",
    "Installer": "section.installer",
    "Settings": "section.settings",
    "Transkription korrigieren": "section.transcription_correction",
    "Help": "section.help",
}

#: Ordered list of section keys (matches SECTIONS order).
_SECTION_KEYS = tuple(_SECTION_T_KEYS.keys())


# ---------------------------------------------------------------------------
# Worker threads for long-running operations
# ---------------------------------------------------------------------------

if QT_AVAILABLE:

    class ServiceWorker(QObject):
        """Runs a service method in a worker thread and emits the result."""

        finished = Signal(object)
        error = Signal(str)

        def __init__(self, func, *args, **kwargs) -> None:
            super().__init__()
            self._func = func
            self._args = args
            self._kwargs = kwargs

        def run(self) -> None:
            try:
                result = self._func(*self._args, **self._kwargs)
                self.finished.emit(result)
            except Exception as exc:  # pragma: no cover - runtime dependent  # noqa: BLE001
                self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Correction image widgets
# ---------------------------------------------------------------------------

if QT_AVAILABLE:

    class _WordOverlay(QLabel):
        """A clickable word bounding box drawn on top of the page image.

        Emits ``clicked`` with the word dict when the user clicks the overlay.
        """

        clicked = Signal(object)  # carries the word dict

        def __init__(self, word: dict[str, Any], parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._word = word
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setStyleSheet(
                "QLabel { background: rgba(197, 160, 89, 0.35); "
                "border: 1px solid rgba(197, 160, 89, 0.8); "
                "border-radius: 3px; color: #1a1410; font-size: 10px; "
                "padding: 1px 2px; }"
            )
            self.setToolTip(word.get("text", ""))
            self.setWord(word.get("text", ""))
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        def setWord(self, text: str) -> None:
            self.setText(text[:12] + ("…" if len(text) > 12 else ""))

        def mousePressEvent(self, event: Any) -> None:  # type: ignore[override]
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self._word)
            super().mousePressEvent(event)

    class _CorrectionImageLabel(QLabel):
        """QLabel that displays a page image with clickable word overlays.

        Overlays are positioned in image-pixel coordinates and scaled with
        the pixmap. Clicking an overlay emits ``word_clicked`` with the word dict.
        """

        word_clicked = Signal(object)  # carries the word dict

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._pixmap: QPixmap | None = None
            self._overlays: list[_WordOverlay] = []
            self.setScaledContents(False)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def setPixmap(self, pixmap: QPixmap | None) -> None:  # type: ignore[override]
            self._pixmap = pixmap
            if pixmap is not None:
                super().setPixmap(pixmap)
            self._reposition_overlays()

        def clear_overlays(self) -> None:
            for ov in self._overlays:
                ov.deleteLater()
            self._overlays.clear()

        def add_word_overlays(self, words: list[dict[str, Any]]) -> None:
            self.clear_overlays()
            for word in words:
                x = int(word.get("x", 0))
                y = int(word.get("y", 0))
                w = int(word.get("width", 0))
                h = int(word.get("height", 0))
                if w <= 0 or h <= 0:
                    continue
                ov = _WordOverlay(word, self)
                ov.clicked.connect(self.word_clicked.emit)
                ov.move(x, y)
                ov.resize(w, h)
                ov.show()
                self._overlays.append(ov)

        def _reposition_overlays(self) -> None:
            if self._pixmap is None or not self.pixmap():
                return
            label_w = self.pixmap().width()
            label_h = self.pixmap().height()
            if label_w <= 0 or label_h <= 0:
                return
            scale_x = self.width() / label_w if self.width() > 0 else 1.0
            scale_y = self.height() / label_h if self.height() > 0 else 1.0
            for ov in self._overlays:
                word = ov._word
                x = int(word.get("x", 0) * scale_x)
                y = int(word.get("y", 0) * scale_y)
                w = int(word.get("width", 0) * scale_x)
                h = int(word.get("height", 0) * scale_y)
                ov.move(x, y)
                ov.resize(max(w, 1), max(h, 1))

        def resizeEvent(self, event: Any) -> None:  # type: ignore[override]
            super().resizeEvent(event)
            self._reposition_overlays()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

if QT_AVAILABLE:

    class MainWindow(QMainWindow):
        """Desktop workbench backed by the public toolkit service layer."""

        def __init__(self) -> None:
            super().__init__()
            self.service = MatriculaDesktopService(
                base_url="", book="01519", out_dir="./downloads"
            )
            translator.set_language(self.service.language)
            self.service.language = translator.get_language()
            self._current_pixmap: QPixmap | None = None
            self._original_pixmap: QPixmap | None = None
            self._zoom_factor = 1.0
            self._rotation_angle = 0
            self._fit_mode: str = "none"  # "none", "fit_window", "fit_width"
            self._is_fullscreen = False
            self._image_list: list[dict[str, Any]] = []
            self._current_image_index = -1
            self._worker_thread: QThread | None = None
            self._worker: ServiceWorker | None = None
            self._refresh_timer: Any | None = None
            #: Labels whose text follows language changes: (label, key, kwargs, last text)
            self._i18n_labels: list[tuple[QLabel, str, dict[str, Any], str]] = []

            self.setWindowTitle(t("window.title"))
            self.resize(1280, 760)
            self.setMinimumSize(900, 560)
            self._build_ui()
            self._retranslate_ui()
            self._refresh_dashboard()
            self._refresh_books()
            self._refresh_sources()
            self._start_resource_timer()

        # ------------------------------------------------------------------
        # Translation helpers
        # ------------------------------------------------------------------
        def _t_label(self, key: str, **kwargs: Any) -> QLabel:
            """Create a QLabel whose text is refreshed when the language changes."""
            label = QLabel(t(key, **kwargs))
            self._register_t_label(label, key, **kwargs)
            return label

        def _register_t_label(self, label: QLabel, key: str, **kwargs: Any) -> None:
            """Register ``label`` for retranslation while it keeps its initial text.

            Labels that show a dynamic result (e.g. a download outcome) are left
            alone once their text differs from the initial translation; their
            owning refresh method renders them again in the new language.
            """
            self._i18n_labels.append((label, key, kwargs, label.text()))

        def _set_t_label(self, label: QLabel, key: str, **kwargs: Any) -> None:
            """Set ``label`` from a translation key and remember it for language changes."""
            text = t(key, **kwargs)
            label.setText(text)
            self._i18n_labels = [
                entry for entry in self._i18n_labels if entry[0] is not label
            ]
            self._i18n_labels.append((label, key, kwargs, text))

        def _select_language_in_combo(self, lang_code: str) -> None:
            """Select ``lang_code`` in the language combo without emitting signals."""
            index = self._lang_combo.findData(lang_code)
            if index < 0:
                index = self._lang_combo.findData(DEFAULT_LANGUAGE)
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(index)
            self._lang_combo.blockSignals(False)

        def _build_ui(self) -> None:
            root = QWidget()
            root.setObjectName("root")
            layout = QHBoxLayout(root)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            # Navigation sidebar
            navigation = QFrame()
            navigation.setObjectName("navigation")
            navigation.setFixedWidth(210)
            nav_layout = QVBoxLayout(navigation)
            nav_layout.setContentsMargins(18, 24, 12, 18)
            brand = QLabel("MATRICULA\nTOOLKIT")
            brand.setObjectName("brand")
            nav_layout.addWidget(brand)
            nav_layout.addSpacing(18)
            self.navigation = QListWidget()
            self.navigation.setAccessibleName(t("a11y.navigation"))
            for section in SECTIONS:
                self.navigation.addItem(t(_SECTION_T_KEYS[section]))
            nav_layout.addWidget(self.navigation)
            layout.addWidget(navigation)

            # Main workspace
            self.workspace = QStackedWidget()
            self.workspace.setObjectName("workspace")
            self._pages: dict[str, QWidget] = {}
            self._page_titles: dict[str, QLabel] = {}
            for section in SECTIONS:
                self._pages[section] = self._page(section)
                self.workspace.addWidget(self._pages[section])
            layout.addWidget(self.workspace, 1)

            # Context panel
            context = QFrame()
            context.setObjectName("contextPanel")
            context.setFixedWidth(260)
            context_layout = QVBoxLayout(context)
            context_layout.setContentsMargins(20, 24, 20, 18)
            self._ctx_title = self._t_label("context.title")
            self._ctx_title.setObjectName("eyebrow")
            context_layout.addWidget(self._ctx_title)
            self._ctx_version = self._t_label("context.version", version=__version__)
            context_layout.addWidget(self._ctx_version)
            context_layout.addSpacing(20)
            self._ctx_book = self._t_label("context.book", book=self.service.book)
            self._ctx_dir = self._t_label("context.dir", directory=self.service.out_dir)
            self._ctx_status = self._t_label("context.status")
            selected = self.service.get_selected_provider()
            if selected is None:
                self._ctx_provider = self._t_label("context.provider_none")
            else:
                self._ctx_provider = self._t_label(
                    "context.provider",
                    name=selected["name"],
                    status=selected["verification_status"],
                )
            context_layout.addWidget(self._ctx_book)
            context_layout.addWidget(self._ctx_dir)
            context_layout.addWidget(self._ctx_status)
            context_layout.addWidget(self._ctx_provider)
            self._ctx_ocr = self._t_label(
                "context.ocr", status=t("context.ocr_not_detected")
            )
            context_layout.addWidget(self._ctx_ocr)
            context_layout.addStretch()
            layout.addWidget(context)

            self.setCentralWidget(root)
            self.statusBar().showMessage(t("status.ready"))
            self.statusBar().setAccessibleName(t("a11y.status_bar"))
            self._status_permanent = self._t_label("status.offline")
            self.statusBar().addPermanentWidget(self._status_permanent)

            self.navigation.currentRowChanged.connect(self.workspace.setCurrentIndex)
            self.navigation.setCurrentRow(0)
            self._set_theme()
            self._update_context_panel()
            self._refresh_provider_combo()

        def _page(self, section: str) -> QWidget:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(42, 34, 42, 34)
            page_layout.setSpacing(14)

            title = QLabel(t(_SECTION_T_KEYS[section]))
            title.setObjectName("pageTitle")
            page_layout.addWidget(title)
            self._page_titles[section] = title

            subtitle = self._t_label("page.subtitle")
            subtitle.setObjectName("subtitle")
            page_layout.addWidget(subtitle)

            if section == "Dashboard":
                page_layout.addWidget(self._dashboard_section())
            elif section == "Research":
                page_layout.addWidget(self._research_section())
            elif section == "Books":
                page_layout.addWidget(self._books_section())
            elif section == "Image browser":
                page_layout.addWidget(self._image_browser_section())
            elif section == "Hit checking":
                page_layout.addWidget(self._hit_checking_section())
            elif section == "Sources":
                page_layout.addWidget(self._sources_section())
            elif section == "OCR jobs":
                page_layout.addWidget(self._ocr_section())
            elif section == "System":
                page_layout.addWidget(self._system_section())
            elif section == "Installer":
                page_layout.addWidget(self._installer_section())
            elif section == "Settings":
                page_layout.addWidget(self._settings_section())
            elif section == "Transkription korrigieren":
                page_layout.addWidget(self._transcription_correction_section())
            elif section == "Help":
                page_layout.addWidget(self._help_section())
            else:
                page_layout.addWidget(self._t_label("page.not_implemented"))

            page_layout.addStretch()
            return page

        # ------------------------------------------------------------------
        # Context panel updates
        # ------------------------------------------------------------------
        def _update_context_panel(self) -> None:
            self._set_t_label(self._ctx_book, "context.book", book=self.service.book)
            self._set_t_label(
                self._ctx_dir, "context.dir", directory=self.service.out_dir
            )
            status = self.service.ocr_backend_status()
            backend = status.get("backend", "not-configured")
            self._set_t_label(self._ctx_ocr, "context.ocr", status=backend)
            selected = self.service.get_selected_provider()
            if selected is None:
                self._set_t_label(self._ctx_provider, "context.provider_none")
            else:
                self._set_t_label(
                    self._ctx_provider,
                    "context.provider",
                    name=selected["name"],
                    status=selected["verification_status"],
                )

        # ------------------------------------------------------------------
        # Dashboard
        # ------------------------------------------------------------------
        def _dashboard_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            self._dashboard_list = QListWidget()
            self._dashboard_list.setObjectName("dashboardList")
            self._dashboard_list.setAlternatingRowColors(True)
            for label in ("Downloads", "Images", "OCR jobs", "CPU"):
                item = QListWidgetItem(
                    f"{t('dashboard.' + label.lower().replace(' ', '_'))}: 0"
                )
                item.setData(Qt.ItemDataRole.UserRole, label)
                self._dashboard_list.addItem(item)

            layout.addWidget(self._dashboard_list)

            self._dash_note = self._t_label("dashboard.note")
            self._dash_note.setObjectName("subtitle")
            self._dash_note.setWordWrap(True)
            layout.addWidget(self._dash_note)
            return panel

        def _refresh_dashboard(self) -> None:
            history = self.service.download_history()
            images = self.service.list_images()
            ocr_jobs = self.service.list_ocr_jobs()
            snapshot = self.service.resource_snapshot()

            values = {
                "Downloads": str(len(history)),
                "Images": str(len(images)),
                "OCR jobs": str(len(ocr_jobs)),
                "CPU": f"{snapshot.cpu_percent} %",
            }
            for i in range(self._dashboard_list.count()):
                item = self._dashboard_list.item(i)
                label = item.data(Qt.ItemDataRole.UserRole)
                item.setText(
                    f"{t('dashboard.' + label.lower().replace(' ', '_'))}: {values[label]}"
                )

        # ------------------------------------------------------------------
        # Research
        # ------------------------------------------------------------------
        def _research_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            # Configuration form
            form = QWidget()
            form_layout = QVBoxLayout(form)
            form_layout.setSpacing(8)

            self._res_url = QLineEdit(
                self.service.base_url or "https://data.matricula-online.eu/de/"
            )
            self._res_book = QLineEdit(self.service.book)
            self._res_dir = QLineEdit(self.service.out_dir)

            self._res_url_label = self._t_label("research.base_url")
            form_layout.addWidget(self._res_url_label)
            form_layout.addWidget(self._res_url)
            self._res_book_label = self._t_label("research.book_id")
            form_layout.addWidget(self._res_book_label)
            form_layout.addWidget(self._res_book)
            self._res_dir_label = self._t_label("research.download_dir")
            form_layout.addWidget(self._res_dir_label)
            form_layout.addWidget(self._res_dir)
            layout.addWidget(form)

            # Provider selection
            provider_row = QHBoxLayout()
            self._res_provider_label = self._t_label("research.provider")
            provider_row.addWidget(self._res_provider_label)
            self._res_provider = QComboBox()
            self._res_provider.setMinimumWidth(220)
            self._res_provider.addItem(t("research.provider_none"), None)
            provider_row.addWidget(self._res_provider)
            provider_row.addStretch()
            layout.addLayout(provider_row)

            # Provider status
            self._res_provider_status = self._t_label(
                "research.provider_none_selected_full"
            )
            self._res_provider_status.setObjectName("providerStatus")
            layout.addWidget(self._res_provider_status)

            # Initialize provider combo after status label exists
            self._refresh_provider_combo()

            # Action buttons
            btn_row = QHBoxLayout()
            self._btn_validate = QPushButton(t("research.btn_validate"))
            self._btn_download_page = QPushButton(t("research.btn_download_page"))
            self._btn_download_range = QPushButton(t("research.btn_download_range"))
            self._btn_save_settings = QPushButton(t("research.btn_save_settings"))
            btn_row.addWidget(self._btn_validate)
            btn_row.addWidget(self._btn_download_page)
            btn_row.addWidget(self._btn_download_range)
            btn_row.addWidget(self._btn_save_settings)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            # Page range inputs
            range_row = QHBoxLayout()
            self._res_from_label = self._t_label("research.from")
            range_row.addWidget(self._res_from_label)
            self._res_start = QLineEdit("1")
            self._res_start.setFixedWidth(60)
            self._res_end = QLineEdit("10")
            self._res_end.setFixedWidth(60)
            range_row.addWidget(self._res_start)
            range_row.addWidget(self._t_label("research.to"))
            range_row.addWidget(self._res_end)
            range_row.addStretch()
            layout.addLayout(range_row)

            # Progress and result
            self._res_progress = QProgressBar()
            self._res_progress.setRange(0, 0)
            self._res_progress.hide()
            layout.addWidget(self._res_progress)

            self._res_result = self._t_label("research.no_action")
            self._res_result.setWordWrap(True)
            self._res_result.setObjectName("researchResult")
            layout.addWidget(self._res_result)

            # Wire signals
            self._btn_validate.clicked.connect(self._on_validate)
            self._btn_download_page.clicked.connect(self._on_download_page)
            self._btn_download_range.clicked.connect(self._on_download_range)
            self._btn_save_settings.clicked.connect(self._on_save_settings)
            self._res_provider.currentIndexChanged.connect(self._on_provider_changed)

            return panel

        def _on_validate(self) -> None:
            self.service.base_url = self._res_url.text().strip()
            self.service.book = self._res_book.text().strip() or self.service.book
            self.service.out_dir = self._res_dir.text().strip() or self.service.out_dir
            self._update_context_panel()
            provider_id = self.service.selected_provider_id
            if provider_id:
                provider = self.service.get_provider(provider_id)
                if provider and provider["entry_hosts"]:
                    host = provider["entry_hosts"][0]
                    browse_check = self.service.can_browse(provider_id, host)
                    if not browse_check["allowed"]:
                        self._set_t_label(
                            self._res_result,
                            "research.browser_denied",
                            reason=browse_check.get(
                                "reason", browse_check.get("error", "unknown")
                            ),
                        )
                        return
            self._run_service(self.service.inspect_book, self._on_validate_result)

        def _on_validate_result(self, result: dict[str, Any]) -> None:
            if result.get("success"):
                self._set_t_label(
                    self._res_result,
                    "research.book_reachable",
                    book=self.service.book,
                    file_path=result.get("file_path") or "downloaded",
                    size_kb=result.get("file_size_bytes", 0) / 1024,
                )
                self._refresh_dashboard()
                self._refresh_books()
            else:
                self._set_t_label(
                    self._res_result,
                    "research.validation_failed",
                    error=result.get("error") or "unknown error",
                )

        def _on_download_page(self) -> None:
            try:
                page = int(self._res_start.text().strip() or "1")
            except ValueError:
                self._set_t_label(self._res_result, "research.invalid_page")
                return
            self._res_progress.show()
            self._btn_download_page.setEnabled(False)
            provider_id = self.service.selected_provider_id
            if provider_id:
                self._run_service(
                    self.service.download_page_with_provider,
                    self._on_download_result,
                    page,
                    provider_id,
                )
            else:
                self._run_service(
                    self.service.download_page,
                    self._on_download_result,
                    page,
                )

        def _on_download_range(self) -> None:
            try:
                start = int(self._res_start.text().strip() or "1")
                end = int(self._res_end.text().strip() or "10")
            except ValueError:
                self._set_t_label(self._res_result, "research.invalid_range")
                return
            if end < start:
                self._set_t_label(self._res_result, "research.end_before_start")
                return
            self._res_progress.show()
            self._btn_download_range.setEnabled(False)
            provider_id = self.service.selected_provider_id
            if provider_id:
                self._run_service(
                    self.service.download_range_with_provider,
                    self._on_download_result,
                    start,
                    end,
                    provider_id,
                )
            else:
                self._run_service(
                    self.service.download_range,
                    self._on_download_result,
                    start,
                    end,
                )

        def _on_download_result(self, result: dict[str, Any]) -> None:
            self._res_progress.hide()
            self._btn_download_page.setEnabled(True)
            self._btn_download_range.setEnabled(True)

            if result.get("success"):
                if "total" in result:
                    self._set_t_label(
                        self._res_result,
                        "research.downloaded_range",
                        success=result["success"],
                        total=result["total"],
                        start=result["start"],
                        end=result["end"],
                    )
                else:
                    self._set_t_label(
                        self._res_result,
                        "research.page_downloaded",
                        page_number=result.get("page_number"),
                        file_path=result.get("file_path") or "n/a",
                        size_kb=result.get("file_size_bytes", 0) / 1024,
                    )
                self._refresh_dashboard()
                self._refresh_books()
                self._refresh_sources()
            else:
                self._set_t_label(
                    self._res_result,
                    "research.download_failed",
                    error=result.get("error") or "unknown error",
                )

        def _on_save_settings(self) -> None:
            self.service.base_url = self._res_url.text().strip()
            self.service.book = self._res_book.text().strip() or self.service.book
            self.service.out_dir = self._res_dir.text().strip() or self.service.out_dir
            self.service.save_settings()
            self._update_context_panel()
            self._set_t_label(self._res_result, "research.settings_saved")

        # ------------------------------------------------------------------
        # Provider integration
        # ------------------------------------------------------------------
        def _refresh_provider_combo(self) -> None:
            """Rebuild the provider combo box from the service's provider list."""
            current_id = self._res_provider.currentData()
            self._res_provider.blockSignals(True)
            self._res_provider.clear()
            self._res_provider.addItem(t("research.provider_none"), None)
            for provider in self.service.get_enabled_providers():
                self._res_provider.addItem(
                    f"{provider['name']} ({provider['id']})",
                    provider["id"],
                )
            # Restore selection if still valid
            if current_id is not None:
                idx = self._res_provider.findData(current_id)
                if idx >= 0:
                    self._res_provider.setCurrentIndex(idx)
            self._res_provider.blockSignals(False)
            self._update_provider_status()

        def _on_provider_changed(self, index: int) -> None:
            """Handle provider selection change in the combo box."""
            provider_id = self._res_provider.itemData(index)
            if provider_id is None:
                # Manual URL mode
                self.service.selected_provider_id = None
                self._res_url.setEnabled(True)
                self._update_provider_status()
                return

            result = self.service.select_provider(provider_id)
            if not result.get("success"):
                self._set_t_label(
                    self._res_result,
                    "research.provider_selection_failed",
                    error=result.get("error"),
                )
                # Revert to no provider
                self._res_provider.setCurrentIndex(0)
                return

            provider = self.service.get_provider(provider_id)
            if provider:
                # Pre-fill base URL from the provider's first entry host
                if provider["entry_hosts"]:
                    self._res_url.setText(f"https://{provider['entry_hosts'][0]}")
                self._res_url.setEnabled(False)
            self._update_provider_status()
            self._set_t_label(
                self._res_result,
                "research.provider_selected",
                name=provider["name"] if provider else provider_id,
            )

        def _update_provider_status(self) -> None:
            """Update the provider status label and context panel."""
            selected = self.service.get_selected_provider()
            if selected is None:
                self._set_t_label(
                    self._res_provider_status, "research.provider_none_selected_full"
                )
                self._res_provider_status.setObjectName("providerStatusNone")
            else:
                caps = []
                if selected["enabled"]:
                    caps.append(t("research.cap_enabled"))
                else:
                    caps.append(t("research.cap_disabled"))
                caps.append(
                    t("research.cap_verified", status=selected["verification_status"])
                )
                caps.append(
                    t("research.cap_image_access", access=selected["image_access"])
                )
                self._set_t_label(
                    self._res_provider_status,
                    "research.provider_status",
                    name=selected["name"],
                    caps=", ".join(caps),
                )
                self._res_provider_status.setObjectName("providerStatusActive")

            # Update context panel
            if hasattr(self, "_ctx_provider"):
                if selected is None:
                    self._set_t_label(self._ctx_provider, "context.provider_none")
                else:
                    self._set_t_label(
                        self._ctx_provider,
                        "context.provider",
                        name=selected["name"],
                        status=selected["verification_status"],
                    )
            self._res_provider_status.style().unpolish(self._res_provider_status)
            self._res_provider_status.style().polish(self._res_provider_status)

        # ------------------------------------------------------------------
        # Books (image list)
        # ------------------------------------------------------------------
        def _books_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            self._books_list = QListWidget()
            self._books_list.setAccessibleName(t("a11y.image_list"))
            self._books_list.itemDoubleClicked.connect(
                self._on_book_item_double_clicked
            )
            layout.addWidget(self._books_list)

            btn_row = QHBoxLayout()
            self._btn_refresh_books = QPushButton(t("books.refresh_list"))
            self._btn_verify_selected = QPushButton(t("books.verify_selected"))
            btn_row.addWidget(self._btn_refresh_books)
            btn_row.addWidget(self._btn_verify_selected)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            self._books_result = self._t_label("books.no_images_yet")
            self._books_result.setWordWrap(True)
            layout.addWidget(self._books_result)

            self._btn_refresh_books.clicked.connect(self._refresh_books)
            self._btn_verify_selected.clicked.connect(self._on_verify_selected)

            return panel

        def _refresh_books(self) -> None:
            self._books_list.clear()
            self._image_list = self.service.list_images()
            for img in self._image_list:
                self._books_list.addItem(
                    t(
                        "books.image_label",
                        page=img["page_number"],
                        size_kb=img["size_bytes"] / 1024,
                    )
                )
            self._set_t_label(
                self._books_result,
                "books.image_count",
                count=len(self._image_list),
                out_dir=self.service.out_dir,
            )

        def _on_book_item_double_clicked(self, item: Any) -> None:
            row = self._books_list.currentRow()
            if 0 <= row < len(self._image_list):
                img = self._image_list[row]
                self._open_image_browser(img["path"], img["page_number"])

        def _on_verify_selected(self) -> None:
            row = self._books_list.currentRow()
            if row < 0 or row >= len(self._image_list):
                self._set_t_label(self._books_result, "books.no_image_selected")
                return
            img = self._image_list[row]
            self._run_service(
                self.service.verify_image,
                self._on_verify_result,
                img["path"],
            )

        def _on_verify_result(self, result: dict[str, Any]) -> None:
            if result.get("valid"):
                self._set_t_label(
                    self._books_result, "books.valid_jpeg", path=result.get("path")
                )
            else:
                self._set_t_label(
                    self._books_result,
                    "books.invalid_or_missing",
                    path=result.get("path"),
                    error=result.get("error"),
                )

        # ------------------------------------------------------------------
        # Image browser
        # ------------------------------------------------------------------
        def _image_browser_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            # Image list (left)
            list_panel = QWidget()
            list_layout = QVBoxLayout(list_panel)
            list_layout.setContentsMargins(0, 0, 0, 0)
            list_layout.addWidget(self._t_label("image_browser.images"))
            self._browser_list = QListWidget()
            self._browser_list.setMaximumWidth(200)
            self._browser_list.currentRowChanged.connect(self._on_browser_row_changed)
            list_layout.addWidget(self._browser_list)
            splitter.addWidget(list_panel)

            # Image view (right)
            view_panel = QWidget()
            view_layout = QVBoxLayout(view_panel)
            view_layout.setContentsMargins(12, 12, 12, 12)
            view_layout.setSpacing(8)

            info_row = QHBoxLayout()
            self._browser_info = self._t_label("image_browser.no_image_selected")
            self._browser_info.setObjectName("subtitle")
            info_row.addWidget(self._browser_info)
            info_row.addStretch()
            view_layout.addLayout(info_row)

            # Source info panel
            self._browser_source = QLabel("")
            self._browser_source.setObjectName("subtitle")
            self._browser_source.setWordWrap(True)
            self._browser_source.setFixedHeight(60)
            view_layout.addWidget(self._browser_source)

            self._image_scroll = QScrollArea()
            self._image_scroll.setWidgetResizable(True)
            self._image_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._image_label = self._t_label("image_browser.no_image_loaded")
            self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._image_label.setObjectName("imageView")
            self._image_scroll.setWidget(self._image_label)
            view_layout.addWidget(self._image_scroll, 1)

            # Navigation + zoom row
            nav_row = QHBoxLayout()
            self._btn_prev = QPushButton(t("image_browser.previous"))
            self._btn_next = QPushButton(t("image_browser.next"))
            nav_row.addWidget(self._btn_prev)
            nav_row.addWidget(self._btn_next)
            nav_row.addStretch()
            view_layout.addLayout(nav_row)

            # Transform row: rotation, fit, fullscreen
            transform_row = QHBoxLayout()
            self._btn_rotate_ccw = QPushButton(t("image_browser.rotate_ccw"))
            self._btn_rotate_ccw.setToolTip(t("image_browser.rotate_ccw_tip"))
            self._btn_rotate_cw = QPushButton(t("image_browser.rotate_cw"))
            self._btn_rotate_cw.setToolTip(t("image_browser.rotate_cw_tip"))
            self._btn_fit_window = QPushButton(t("image_browser.fit_window"))
            self._btn_fit_window.setCheckable(True)
            self._btn_fit_width = QPushButton(t("image_browser.fit_width"))
            self._btn_fit_width.setCheckable(True)
            self._btn_fullscreen = QPushButton(t("image_browser.fullscreen"))
            self._btn_fullscreen.setCheckable(True)
            self._btn_zoom_out = QPushButton(t("image_browser.zoom_out"))
            self._btn_zoom_reset = QPushButton(t("image_browser.zoom_reset"))
            self._btn_zoom_in = QPushButton(t("image_browser.zoom_in"))
            transform_row.addWidget(self._btn_rotate_ccw)
            transform_row.addWidget(self._btn_rotate_cw)
            transform_row.addStretch()
            transform_row.addWidget(self._btn_fit_window)
            transform_row.addWidget(self._btn_fit_width)
            transform_row.addWidget(self._btn_fullscreen)
            transform_row.addStretch()
            transform_row.addWidget(self._btn_zoom_out)
            transform_row.addWidget(self._btn_zoom_reset)
            transform_row.addWidget(self._btn_zoom_in)
            view_layout.addLayout(transform_row)

            self._browser_status = QLabel("")
            self._browser_status.setObjectName("subtitle")
            view_layout.addWidget(self._browser_status)

            splitter.addWidget(view_panel)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            layout.addWidget(splitter)

            # Wire signals
            self._btn_zoom_in.clicked.connect(self._zoom_in)
            self._btn_zoom_out.clicked.connect(self._zoom_out)
            self._btn_zoom_reset.clicked.connect(self._zoom_reset)
            self._btn_prev.clicked.connect(self._prev_image)
            self._btn_next.clicked.connect(self._next_image)
            self._btn_rotate_ccw.clicked.connect(self._rotate_ccw)
            self._btn_rotate_cw.clicked.connect(self._rotate_cw)
            self._btn_fit_window.clicked.connect(self._toggle_fit_window)
            self._btn_fit_width.clicked.connect(self._toggle_fit_width)
            self._btn_fullscreen.clicked.connect(self._toggle_fullscreen)

            self._refresh_browser_list()
            return panel

        def _refresh_browser_list(self) -> None:
            self._browser_list.clear()
            self._image_list = self.service.list_images()
            for img in self._image_list:
                self._browser_list.addItem(f"pg{img['page_number']:03d}")
            if self._image_list:
                self._browser_list.setCurrentRow(0)

        def _on_browser_row_changed(self, row: int) -> None:
            if 0 <= row < len(self._image_list):
                img = self._image_list[row]
                self._load_image(img["path"], img["page_number"])

        def _load_image(self, path: str, page_number: int) -> None:
            self._current_image_index = self._image_list.index(
                next(i for i in self._image_list if i["path"] == path)
            )
            self._browser_list.setCurrentRow(self._current_image_index)
            self._zoom_factor = 1.0
            self._rotation_angle = 0
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)

            pixmap = QPixmap(path)
            if pixmap.isNull():
                self._set_t_label(
                    self._image_label, "image_browser.cannot_load", path=path
                )
                self._image_label.setPixmap(QPixmap())
                self._current_pixmap = None
                self._original_pixmap = None
                self._set_t_label(
                    self._browser_info,
                    "image_browser.page_load_failed",
                    page_number=page_number,
                )
                self._browser_status.setText("")
                self._browser_source.setText("")
                return
            self._original_pixmap = pixmap
            self._current_pixmap = pixmap
            self._apply_transform()
            self._set_t_label(
                self._browser_info,
                "image_browser.page_info",
                page_number=page_number,
                width=pixmap.width(),
                height=pixmap.height(),
            )
            self._set_t_label(self._browser_status, "image_browser.file", path=path)
            self._show_source_info(page_number, path)

        def _show_source_info(self, page_number: int, file_path: str) -> None:
            """Display source metadata for the current image, if available."""
            for source in self.service.list_sources():
                if (
                    source["book"] == self.service.book
                    and source["page"] == page_number
                ):
                    parts = []
                    if source["archive"]:
                        parts.append(
                            t("image_browser.source_archive", archive=source["archive"])
                        )
                    if source["municipality"]:
                        parts.append(
                            t(
                                "image_browser.source_municipality",
                                municipality=source["municipality"],
                            )
                        )
                    if source["dating"]:
                        parts.append(
                            t("image_browser.source_dating", dating=source["dating"])
                        )
                    if source["signature"]:
                        parts.append(
                            t(
                                "image_browser.source_signature",
                                signature=source["signature"],
                            )
                        )
                    if source["notes"]:
                        parts.append(
                            t("image_browser.source_notes", notes=source["notes"])
                        )
                    if parts:
                        self._browser_source.setText(t(" | ").join(parts))
                    else:
                        self._set_t_label(
                            self._browser_source, "image_browser.no_source_metadata"
                        )
                    return
            self._set_t_label(
                self._browser_source, "image_browser.no_source_metadata_for_page"
            )

        def _open_image_browser(self, path: str, page_number: int) -> None:
            # Switch to image browser section and load the image
            idx = SECTIONS.index("Image browser")
            self.navigation.setCurrentRow(idx)
            self.workspace.setCurrentIndex(idx)
            self._load_image(path, page_number)

        def _zoom_in(self) -> None:
            if not self._current_pixmap:
                return
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)
            self._zoom_factor = min(self._zoom_factor * 1.25, 4.0)
            self._apply_transform()

        def _zoom_out(self) -> None:
            if not self._current_pixmap:
                return
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)
            self._zoom_factor = max(self._zoom_factor / 1.25, 0.25)
            self._apply_transform()

        def _zoom_reset(self) -> None:
            if not self._current_pixmap:
                return
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)
            self._zoom_factor = 1.0
            self._apply_transform()

        def _apply_transform(self) -> None:
            if not self._original_pixmap:
                return
            pixmap = self._original_pixmap

            # Apply rotation
            if self._rotation_angle != 0:
                transform = QTransform().rotate(self._rotation_angle)
                pixmap = pixmap.transformed(
                    transform, Qt.TransformationMode.SmoothTransformation
                )

            # Apply fit mode or zoom
            if self._fit_mode == "fit_window":
                available = self._image_scroll.viewport().size()
                pixmap = pixmap.scaled(
                    available,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            elif self._fit_mode == "fit_width":
                available = self._image_scroll.viewport().size()
                pixmap = pixmap.scaledToWidth(
                    available.width(),
                    Qt.TransformationMode.SmoothTransformation,
                )
            else:
                pixmap = pixmap.scaled(
                    pixmap.size() * self._zoom_factor,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )

            self._current_pixmap = pixmap
            self._image_label.setPixmap(pixmap)
            self._image_label.adjustSize()

        def _rotate_ccw(self) -> None:
            if not self._original_pixmap:
                return
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)
            self._rotation_angle = (self._rotation_angle - 90) % 360
            self._apply_transform()

        def _rotate_cw(self) -> None:
            if not self._original_pixmap:
                return
            self._fit_mode = "none"
            self._btn_fit_window.setChecked(False)
            self._btn_fit_width.setChecked(False)
            self._rotation_angle = (self._rotation_angle + 90) % 360
            self._apply_transform()

        def _toggle_fit_window(self, checked: bool) -> None:
            if not self._original_pixmap:
                return
            if checked:
                self._btn_fit_width.setChecked(False)
                self._fit_mode = "fit_window"
            else:
                self._fit_mode = "none"
            self._apply_transform()

        def _toggle_fit_width(self, checked: bool) -> None:
            if not self._original_pixmap:
                return
            if checked:
                self._btn_fit_window.setChecked(False)
                self._fit_mode = "fit_width"
            else:
                self._fit_mode = "none"
            self._apply_transform()

        def _toggle_fullscreen(self, checked: bool) -> None:
            if checked:
                self.showFullScreen()
            else:
                self.showNormal()
            self._is_fullscreen = checked

        def _prev_image(self) -> None:
            if self._current_image_index > 0:
                self._browser_list.setCurrentRow(self._current_image_index - 1)

        def _next_image(self) -> None:
            if self._current_image_index < len(self._image_list) - 1:
                self._browser_list.setCurrentRow(self._current_image_index + 1)

        # ------------------------------------------------------------------
        # Hit checking
        # ------------------------------------------------------------------
        def _hit_checking_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            intro = self._t_label("hit_checking.intro")
            intro.setWordWrap(True)
            intro.setObjectName("subtitle")
            layout.addWidget(intro)

            # Source info
            self._hit_source = self._t_label("hit_checking.no_source_selected")
            self._hit_source.setWordWrap(True)
            self._hit_source.setObjectName("subtitle")
            layout.addWidget(self._hit_source)

            # Image preview (small, alongside transcription)
            self._hit_image_label = self._t_label("hit_checking.no_image")
            self._hit_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._hit_image_label.setObjectName("imageView")
            self._hit_image_label.setFixedSize(200, 260)
            self._hit_image_label.setStyleSheet(
                "QLabel#imageView { background: #0a1018; border: 1px solid #294763; }"
            )
            layout.addWidget(self._hit_image_label)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            # Original (left)
            orig_panel = QWidget()
            orig_layout = QVBoxLayout(orig_panel)
            self._hit_orig_label = self._t_label("hit_checking.original_transcription")
            orig_layout.addWidget(self._hit_orig_label)
            self._hit_original = QPlainTextEdit()
            self._hit_original.setReadOnly(True)
            self._hit_original.setPlaceholderText(
                t("hit_checking.no_transcription_loaded")
            )
            orig_layout.addWidget(self._hit_original)
            splitter.addWidget(orig_panel)

            # Corrected (right)
            corr_panel = QWidget()
            corr_layout = QVBoxLayout(corr_panel)
            self._hit_corr_label = self._t_label("hit_checking.corrected_transcription")
            corr_layout.addWidget(self._hit_corr_label)
            self._hit_corrected = QPlainTextEdit()
            self._hit_corrected.setPlaceholderText(t("hit_checking.enter_correction"))
            corr_layout.addWidget(self._hit_corrected)
            splitter.addWidget(corr_panel)

            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 1)
            layout.addWidget(splitter, 1)

            # Diff view toggle
            self._btn_show_diff = QPushButton(t("hit_checking.show_diff"))
            self._btn_show_diff.setCheckable(True)
            self._btn_show_diff.clicked.connect(self._toggle_hit_diff)
            layout.addWidget(self._btn_show_diff)

            self._hit_diff = QPlainTextEdit()
            self._hit_diff.setReadOnly(True)
            self._hit_diff.setPlaceholderText(t("hit_checking.diff_placeholder"))
            self._hit_diff.setMaximumHeight(150)
            self._hit_diff.hide()
            layout.addWidget(self._hit_diff)

            # Buttons
            btn_row = QHBoxLayout()
            self._btn_load_hit = QPushButton(t("hit_checking.load_ocr_result"))
            self._btn_save_hit = QPushButton(t("hit_checking.save_correction"))
            self._btn_export_hit = QPushButton(t("hit_checking.export_correction"))
            btn_row.addWidget(self._btn_load_hit)
            btn_row.addWidget(self._btn_save_hit)
            btn_row.addWidget(self._btn_export_hit)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            self._hit_result = QLabel("")
            self._hit_result.setWordWrap(True)
            layout.addWidget(self._hit_result)

            self._btn_load_hit.clicked.connect(self._on_load_hit)
            self._btn_save_hit.clicked.connect(self._on_save_hit)
            self._btn_export_hit.clicked.connect(self._on_export_hit)

            return panel

        def _on_load_hit(self) -> None:
            row = self._books_list.currentRow()
            if row < 0 or row >= len(self._image_list):
                self._set_t_label(self._hit_result, "hit_checking.select_image_first")
                return
            img = self._image_list[row]
            self._set_t_label(
                self._hit_source,
                "hit_checking.source",
                name=img["name"],
                page=img["page_number"],
            )

            # Show image preview
            pixmap = QPixmap(img["path"])
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    180,
                    240,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._hit_image_label.setPixmap(scaled)
                self._hit_image_label.setText("")
            else:
                self._hit_image_label.setText(t("hit_checking.cannot_load_image"))
                self._hit_image_label.setPixmap(QPixmap())

            # Look for OCR result
            found = False
            for job in self.service.list_ocr_jobs():
                if (
                    job.get("source_file") == img["path"]
                    and job.get("status") == "success"
                ):
                    output_dir = Path(job.get("output_dir", ""))
                    text_files = (
                        sorted(output_dir.glob("*.txt")) if output_dir.is_dir() else []
                    )
                    if text_files:
                        self._hit_original.setPlainText(
                            text_files[0].read_text(encoding="utf-8")
                        )
                        found = True
                        break
            if not found:
                self._hit_original.setPlainText(
                    t("hit_checking.no_ocr", page=img["page_number"])
                )
            self._hit_corrected.setPlainText("")
            self._hit_result.setText("")
            self._hit_diff.hide()
            self._btn_show_diff.setChecked(False)

        def _toggle_hit_diff(self, checked: bool) -> None:
            if not checked:
                self._hit_diff.hide()
                return
            original = self._hit_original.toPlainText()
            corrected = self._hit_corrected.toPlainText()
            if not original or not corrected:
                self._hit_diff.setPlainText(t("hit_checking.diff_needs_both"))
                self._hit_diff.show()
                return
            diff_lines = self._compute_diff(original, corrected)
            self._hit_diff.setPlainText("\n".join(diff_lines))
            self._hit_diff.show()

        @staticmethod
        def _compute_diff(original: str, corrected: str) -> list[str]:
            """Simple line-based diff showing added/removed/unchanged lines."""
            import difflib

            orig_lines = original.splitlines()
            corr_lines = corrected.splitlines()
            diff = list(
                difflib.unified_diff(
                    orig_lines,
                    corr_lines,
                    lineterm="",
                    fromfile=t("hit_checking.label_original"),
                    tofile=t("hit_checking.label_corrected"),
                )
            )
            if not diff:
                return [t("hit_checking.no_differences")]
            return diff

        def _on_save_hit(self) -> None:
            row = self._books_list.currentRow()
            if row < 0 or row >= len(self._image_list):
                self._set_t_label(self._hit_result, "hit_checking.no_source")
                return
            img = self._image_list[row]
            corrected = self._hit_corrected.toPlainText().strip()
            if not corrected:
                self._set_t_label(self._hit_result, "hit_checking.nothing_to_save")
                return
            # Save correction alongside the image
            out_dir = Path(self.service.out_dir) / "corrections"
            out_dir.mkdir(parents=True, exist_ok=True)
            correction_file = out_dir / f"{img['name']}.corrected.txt"
            original = self._hit_original.toPlainText()
            record = {
                "source_file": img["path"],
                "page_number": img["page_number"],
                "original_transcription": original,
                "corrected_transcription": corrected,
                "saved_at": datetime.now().isoformat(),
            }
            correction_file.write_text(
                f"# Correction for {img['name']}\n"
                f"# Saved: {record['saved_at']}\n"
                f"\n## Original\n{original}\n"
                f"\n## Corrected\n{corrected}\n",
                encoding="utf-8",
            )
            # Also link/update source record
            self.service.link_source_to_download(img["page_number"], img["path"])
            self._set_t_label(
                self._hit_result, "hit_checking.saved_to", path=correction_file
            )

        def _on_export_hit(self) -> None:
            row = self._books_list.currentRow()
            if row < 0 or row >= len(self._image_list):
                self._set_t_label(self._hit_result, "hit_checking.no_source")
                return
            img = self._image_list[row]
            corrected = self._hit_corrected.toPlainText().strip()
            if not corrected:
                self._set_t_label(self._hit_result, "hit_checking.nothing_to_export")
                return
            result = self.service.export_correction(img["path"])
            if result.get("success"):
                self._set_t_label(
                    self._hit_result,
                    "hit_checking.exported_to",
                    path=result.get("output_path"),
                    bytes=result.get("bytes"),
                )
            else:
                self._set_t_label(
                    self._hit_result,
                    "hit_checking.export_failed",
                    error=result.get("error"),
                )

        # ------------------------------------------------------------------
        # Sources
        # ------------------------------------------------------------------
        def _sources_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            intro = self._t_label("sources.intro")
            intro.setWordWrap(True)
            intro.setObjectName("subtitle")
            layout.addWidget(intro)

            self._sources_list = QListWidget()
            self._sources_list.setAccessibleName(t("sources.list_name"))
            layout.addWidget(self._sources_list, 1)

            # Source metadata editor
            editor = QWidget()
            editor_layout = QGridLayout(editor)
            editor_layout.setSpacing(6)
            editor_layout.addWidget(self._t_label("sources.archive"), 0, 0)
            self._src_archive = QLineEdit()
            editor_layout.addWidget(self._src_archive, 0, 1)
            editor_layout.addWidget(self._t_label("sources.municipality"), 1, 0)
            self._src_municipality = QLineEdit()
            editor_layout.addWidget(self._src_municipality, 1, 1)
            editor_layout.addWidget(self._t_label("sources.dating"), 2, 0)
            self._src_dating = QLineEdit()
            editor_layout.addWidget(self._src_dating, 2, 1)
            editor_layout.addWidget(self._t_label("sources.signature"), 3, 0)
            self._src_signature = QLineEdit()
            editor_layout.addWidget(self._src_signature, 3, 1)
            editor_layout.addWidget(self._t_label("sources.notes"), 4, 0)
            self._src_notes = QLineEdit()
            editor_layout.addWidget(self._src_notes, 4, 1)
            editor_layout.addWidget(self._t_label("sources.status"), 5, 0)
            self._src_status = QLineEdit("unverified")
            editor_layout.addWidget(self._src_status, 5, 1)
            self._btn_save_source = QPushButton(t("sources.save_metadata"))
            editor_layout.addWidget(self._btn_save_source, 6, 0, 1, 2)
            layout.addWidget(editor)

            btn_row = QHBoxLayout()
            self._btn_refresh_sources = QPushButton(t("sources.refresh"))
            self._btn_verify_all = QPushButton(t("sources.verify_all"))
            self._btn_export_sources = QPushButton(t("sources.export_json"))
            self._btn_export_sources_csv = QPushButton(t("sources.export_csv"))
            btn_row.addWidget(self._btn_refresh_sources)
            btn_row.addWidget(self._btn_verify_all)
            btn_row.addStretch()
            btn_row.addWidget(self._btn_export_sources)
            btn_row.addWidget(self._btn_export_sources_csv)
            layout.addLayout(btn_row)

            self._sources_result = QLabel("")
            self._sources_result.setWordWrap(True)
            layout.addWidget(self._sources_result)

            self._btn_refresh_sources.clicked.connect(self._refresh_sources)
            self._btn_verify_all.clicked.connect(self._on_verify_all)
            self._btn_export_sources.clicked.connect(self._on_export_sources_json)
            self._btn_export_sources_csv.clicked.connect(self._on_export_sources_csv)
            self._btn_save_source.clicked.connect(self._on_save_source_metadata)
            self._sources_list.currentItemChanged.connect(self._on_source_selected)

            return panel

        def _refresh_sources(self) -> None:
            self._sources_list.clear()
            history = self.service.download_history()
            sources = {s["page"]: s for s in self.service.list_sources()}
            for record in history:
                page = record["page_number"]
                status = "OK" if record["success"] else "FAILED"
                size = (
                    f" ({record['file_size_bytes'] / 1024:.1f} KB)"
                    if record["file_size_bytes"]
                    else ""
                )
                src = sources.get(page, {})
                vstatus = src.get("verification_status", "unverified")
                self._sources_list.addItem(
                    t(
                        "sources.record",
                        page=page,
                        status=status,
                        size=size,
                        vstatus=vstatus,
                        file_path=record["file_path"] or "n/a",
                    )
                )
            self._set_t_label(
                self._sources_result, "sources.record_count", count=len(history)
            )

        def _on_source_selected(self, current: Any, previous: Any) -> None:
            if current is None:
                return
            text = current.text()
            import re

            match = re.search(r"pg(\d+)", text)
            if not match:
                return
            page = int(match.group(1))
            sources = {s["page"]: s for s in self.service.list_sources()}
            src = sources.get(page)
            if src:
                self._src_archive.setText(src.get("archive", ""))
                self._src_municipality.setText(src.get("municipality", ""))
                self._src_dating.setText(src.get("dating", ""))
                self._src_signature.setText(src.get("signature", ""))
                self._src_notes.setText(src.get("notes", ""))
                self._src_status.setText(src.get("verification_status", "unverified"))
            else:
                self._src_archive.clear()
                self._src_municipality.clear()
                self._src_dating.clear()
                self._src_signature.clear()
                self._src_notes.clear()
                self._src_status.setText("unverified")

        def _on_save_source_metadata(self) -> None:
            current = self._sources_list.currentItem()
            if current is None:
                self._set_t_label(self._sources_result, "sources.no_source_selected")
                return
            text = current.text()
            import re

            match = re.search(r"pg(\d+)", text)
            if not match:
                return
            page = int(match.group(1))
            sources = {s["page"]: s for s in self.service.list_sources()}
            src = sources.get(page)
            if src:
                self.service.update_source(
                    src["id"],
                    archive=self._src_archive.text().strip(),
                    municipality=self._src_municipality.text().strip(),
                    dating=self._src_dating.text().strip(),
                    signature=self._src_signature.text().strip(),
                    notes=self._src_notes.text().strip(),
                    verification_status=self._src_status.text().strip() or "unverified",
                )
                self._set_t_label(
                    self._sources_result, "sources.metadata_saved", page=page
                )
            else:
                history = self.service.download_history()
                record = next((r for r in history if r["page_number"] == page), None)
                file_path = record["file_path"] if record else None
                self.service.add_source(
                    archive=self._src_archive.text().strip(),
                    municipality=self._src_municipality.text().strip(),
                    book=self.service.book,
                    page=page,
                    dating=self._src_dating.text().strip(),
                    signature=self._src_signature.text().strip(),
                    notes=self._src_notes.text().strip(),
                    file_path=file_path,
                )
                self._set_t_label(self._sources_result, "sources.new_record", page=page)
            self._refresh_sources()

        def _on_export_sources_json(self) -> None:
            data = self.service.export_sources(format="json")
            out_path = Path(self.service.out_dir) / "sources_export.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(data, encoding="utf-8")
            self._set_t_label(
                self._sources_result, "sources.exported_to", path=out_path
            )

        def _on_export_sources_csv(self) -> None:
            data = self.service.export_sources(format="csv")
            out_path = Path(self.service.out_dir) / "sources_export.csv"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(data, encoding="utf-8")
            self._set_t_label(
                self._sources_result, "sources.exported_to", path=out_path
            )

        def _on_verify_all(self) -> None:
            images = self.service.list_images()
            if not images:
                self._set_t_label(self._sources_result, "sources.no_images_to_verify")
                return
            self._verify_index = 0
            self._verify_images = images
            self._verify_next()

        def _verify_next(self) -> None:
            if self._verify_index >= len(self._verify_images):
                self._set_t_label(
                    self._sources_result,
                    "sources.verified_count",
                    count=len(self._verify_images),
                )
                self._refresh_sources()
                return
            img = self._verify_images[self._verify_index]
            self._run_service(
                self.service.verify_image,
                self._on_verify_all_result,
                img["path"],
            )

        def _on_verify_all_result(self, result: dict[str, Any]) -> None:
            self._verify_index += 1
            self._verify_next()

        # ------------------------------------------------------------------
        # OCR jobs
        # ------------------------------------------------------------------
        def _ocr_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            # Backend status
            status = self.service.ocr_backend_status()
            self._ocr_backend = self._t_label(
                "ocr.backend_status",
                backend=status.get("backend", "not-configured"),
                available=status.get("available", False),
                message=status.get("message", ""),
            )
            self._ocr_backend.setWordWrap(True)
            self._ocr_backend.setObjectName("subtitle")
            layout.addWidget(self._ocr_backend)

            # Inventory
            layout.addWidget(self._t_label("ocr.prepared_images"))
            self._ocr_inventory = QListWidget()
            self._ocr_inventory.setMaximumHeight(120)
            layout.addWidget(self._ocr_inventory)

            btn_row = QHBoxLayout()
            self._btn_refresh_ocr = QPushButton(t("ocr.refresh_inventory"))
            self._btn_run_ocr = QPushButton(t("ocr.run_ocr_selected"))
            btn_row.addWidget(self._btn_refresh_ocr)
            btn_row.addWidget(self._btn_run_ocr)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            # Job list
            layout.addWidget(self._t_label("ocr.ocr_jobs"))
            self._ocr_jobs_list = QListWidget()
            layout.addWidget(self._ocr_jobs_list, 1)

            self._ocr_result = QLabel("")
            self._ocr_result.setWordWrap(True)
            layout.addWidget(self._ocr_result)

            self._btn_refresh_ocr.clicked.connect(self._refresh_ocr)
            self._btn_run_ocr.clicked.connect(self._on_run_ocr)

            self._refresh_ocr()
            return panel

        def _refresh_ocr(self) -> None:
            self._ocr_inventory.clear()
            inventory = self.service.prepare_ocr_inventory()
            for f in inventory["files"]:
                self._ocr_inventory.addItem(f)
            self._ocr_inventory_empty = not inventory["files"]
            if self._ocr_inventory_empty:
                self._ocr_inventory.addItem(t("ocr_jobs.no_prepared"))

            self._ocr_jobs_list.clear()
            for job in self.service.list_ocr_jobs():
                self._ocr_jobs_list.addItem(
                    t(
                        "ocr_jobs.job_item",
                        id=job["id"],
                        status=job["status"],
                        source_file=job["source_file"],
                        line_count=job["line_count"],
                    )
                )

            status = self.service.ocr_backend_status()
            self._set_t_label(
                self._ocr_backend,
                "ocr_jobs.backend",
                backend=status.get("backend", "not-configured"),
                available=status.get("available", False),
                message=status.get("message", ""),
            )
            self._btn_run_ocr.setEnabled(status.get("available", False))

        def _on_run_ocr(self) -> None:
            row = self._ocr_inventory.currentRow()
            if row < 0:
                self._set_t_label(self._ocr_result, "ocr_jobs.select_image")
                return
            item = self._ocr_inventory.item(row)
            source_file = item.text()
            if self._ocr_inventory_empty:
                return

            # Find or create a model path
            model_path = self._find_ocr_model()
            if not model_path:
                self._set_t_label(self._ocr_result, "ocr_jobs.no_model")
                return

            job = self.service.add_ocr_job(source_file=source_file, model=model_path)

            # Check system load before starting the OCR job.
            load_check = self.service.check_ocr_load()
            if not load_check["allowed"]:
                self._set_t_label(
                    self._ocr_result, "ocr_jobs.blocked", reason=load_check["reason"]
                )
                self._refresh_ocr()
                return

            self._set_t_label(self._ocr_result, "ocr_jobs.starting", job_id=job.id)
            self._run_service(
                self.service.run_ocr_job,
                self._on_ocr_result,
                job,
                model_path,
                1,
            )

        def _on_ocr_result(self, result: dict[str, Any]) -> None:
            if result.get("success"):
                self._set_t_label(
                    self._ocr_result,
                    "ocr_jobs.complete",
                    line_count=result.get("line_count", 0),
                    output_dir=result.get("output_dir"),
                )
            else:
                self._set_t_label(
                    self._ocr_result, "ocr_jobs.failed", error=result.get("error")
                )
            self._refresh_ocr()

        def _find_ocr_model(self) -> str | None:
            """Look for a Kraken model file in project-local locations."""
            candidates = [
                Path("ocr-models") / "german_handwriting_kurrent_v3.mlmodel",
                Path("models") / "german_handwriting_kurrent_v3.mlmodel",
                Path(".tools")
                / "ocr-data"
                / "htrmopo"
                / "*"
                / "german_handwriting_kurrent_v3.mlmodel",
            ]
            for pattern in candidates:
                matches = list(Path.cwd().glob(str(pattern)))
                if matches:
                    return str(matches[0])
            return None

        # ------------------------------------------------------------------
        # System
        # ------------------------------------------------------------------
        def _system_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            self._sys_cpu = self._t_label("system.cpu", percent="–")
            self._sys_ram = self._t_label("system.ram", percent="–")
            self._sys_temp = self._t_label("system.temperature", celsius="–")
            self._sys_downloads = self._t_label("system.active_downloads", count=0)
            self._sys_ocr_jobs = self._t_label("system.active_ocr_jobs", count=0)

            for w in (
                self._sys_cpu,
                self._sys_ram,
                self._sys_temp,
                self._sys_downloads,
                self._sys_ocr_jobs,
            ):
                w.setObjectName("subtitle")
                layout.addWidget(w)

            self._btn_refresh_sys = QPushButton(t("system.refresh_now"))
            layout.addWidget(self._btn_refresh_sys)
            self._btn_refresh_sys.clicked.connect(self._refresh_system)

            self._refresh_system()
            return panel

        def _refresh_system(self) -> None:
            snapshot = self.service.resource_snapshot()
            self._set_t_label(self._sys_cpu, "system.cpu", percent=snapshot.cpu_percent)
            self._set_t_label(self._sys_ram, "system.ram", percent=snapshot.ram_percent)
            temp = (
                t("system.temperature", celsius=snapshot.temperature_c)
                if snapshot.temperature_c is not None
                else t("system.temp_not_available")
            )
            self._sys_temp.setText(temp)
            self._set_t_label(
                self._sys_downloads,
                "system.active_downloads",
                count=snapshot.active_downloads,
            )
            self._set_t_label(
                self._sys_ocr_jobs,
                "system.active_ocr_jobs",
                count=snapshot.active_ocr_jobs,
            )

        def _start_resource_timer(self) -> None:
            from PySide6.QtCore import QTimer

            self._refresh_timer = QTimer(self)
            self._refresh_timer.timeout.connect(self._refresh_system)
            self._refresh_timer.start(5000)  # every 5 seconds

        # ------------------------------------------------------------------
        # Installer
        # ------------------------------------------------------------------
        def _installer_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            intro = self._t_label("installer.intro")
            intro.setWordWrap(True)
            intro.setObjectName("subtitle")
            layout.addWidget(intro)

            # System info
            self._inst_sys_info = self._t_label("installer.system_not_checked")
            self._inst_sys_info.setWordWrap(True)
            self._inst_sys_info.setObjectName("subtitle")
            layout.addWidget(self._inst_sys_info)

            # Component status
            layout.addWidget(self._t_label("installer.component_status"))
            self._inst_components = QListWidget()
            self._inst_components.setMaximumHeight(150)
            layout.addWidget(self._inst_components)

            # Model selection
            layout.addWidget(self._t_label("installer.kraken_models"))
            self._inst_model_checkboxes: dict[str, QCheckBox] = {}
            for doi, info in KRAKEN_MODELS.items():
                cb = QCheckBox(
                    t(
                        "installer.kraken_model",
                        name=info["name"],
                        script=info["script"],
                        license=info["license"],
                    )
                )
                cb.setToolTip(info["description"])
                cb.setChecked(doi in DEFAULT_KRAKEN_MODELS)
                self._inst_model_checkboxes[doi] = cb
                layout.addWidget(cb)

            # Buttons
            btn_row = QHBoxLayout()
            self._btn_check = QPushButton(t("installer.check_system"))
            self._btn_install = QPushButton(t("installer.install_missing"))
            self._btn_retry = QPushButton(t("installer.retry_failed"))
            self._btn_retry.setEnabled(False)
            btn_row.addWidget(self._btn_check)
            btn_row.addWidget(self._btn_install)
            btn_row.addWidget(self._btn_retry)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            # Progress
            self._inst_progress = QProgressBar()
            self._inst_progress.setRange(0, 100)
            self._inst_progress.setValue(0)
            layout.addWidget(self._inst_progress)

            self._inst_status = self._t_label("installer.ready")
            self._inst_status.setWordWrap(True)
            layout.addWidget(self._inst_status)

            # Log
            layout.addWidget(self._t_label("installer.installation_log"))
            self._inst_log = QPlainTextEdit()
            self._inst_log.setReadOnly(True)
            self._inst_log.setMaximumHeight(200)
            layout.addWidget(self._inst_log, 1)

            # Wire up signals
            self._btn_check.clicked.connect(self._on_installer_check)
            self._btn_install.clicked.connect(self._on_installer_install)
            self._btn_retry.clicked.connect(self._on_installer_retry)

            return panel

        def _on_installer_check(self) -> None:
            """Run system detection and component check (synchronous, fast)."""
            if self._worker_thread is not None and not self._worker_thread.isFinished():
                QMessageBox.warning(
                    self, t("installer.busy_title"), t("installer.busy_message")
                )
                return

            self._set_t_label(self._inst_status, "installer.checking_system")
            self._inst_log.clear()
            self._inst_progress.setValue(0)
            self._inst_components.clear()

            try:
                info = detect_system()
                components = detect_components()

                self._set_t_label(
                    self._inst_sys_info,
                    "installer.system_info",
                    os=info.os,
                    os_version=info.os_version or "",
                    distribution=info.distribution or t("system.not_available"),
                    arch=info.architecture,
                    python=info.python_version,
                    ram=info.ram_total_mb,
                    disk=info.disk_free_mb,
                    gpu=info.gpu_vendor or t("system.not_available"),
                )

                for comp in components:
                    status = (
                        t("installer.ok") if comp.installed else t("installer.missing")
                    )
                    opt = f" ({t('installer.optional')})" if comp.optional else ""
                    item_text = f"{comp.name}: {status}{opt}"
                    if comp.version:
                        item_text += f" ({comp.version})"
                    if comp.error:
                        item_text += f" — {comp.error}"
                    item = QListWidgetItem(item_text)
                    if not comp.installed and not comp.optional:
                        item.setForeground(
                            self.palette().color(self.palette().ColorRole.WindowText)
                        )
                    self._inst_components.addItem(item)

                missing = [c for c in components if not c.installed and not c.optional]
                if missing:
                    self._set_t_label(
                        self._inst_status,
                        "installer.components_missing",
                        count=len(missing),
                    )
                    self._btn_install.setEnabled(True)
                else:
                    self._set_t_label(
                        self._inst_status, "installer.all_components_present"
                    )
                    self._btn_install.setEnabled(False)
                self._btn_retry.setEnabled(False)
                self._inst_progress.setValue(100)
            except Exception as exc:  # noqa: BLE001
                self._set_t_label(
                    self._inst_status, "installer.check_failed", exc=str(exc)
                )
                self._inst_log.appendPlainText(
                    t("installer.error_prefix", error=str(exc))
                )

        def _on_installer_install(self) -> None:
            """Start the installation in a worker thread."""
            if self._worker_thread is not None and not self._worker_thread.isFinished():
                QMessageBox.warning(
                    self, t("installer.busy_title"), t("installer.busy_message")
                )
                return

            selected_models = [
                doi for doi, cb in self._inst_model_checkboxes.items() if cb.isChecked()
            ]

            self._set_t_label(self._inst_status, "installer.installing")
            self._inst_log.clear()
            self._inst_progress.setValue(0)
            self._btn_install.setEnabled(False)
            self._btn_check.setEnabled(False)
            self._btn_retry.setEnabled(False)

            def _install() -> InstallResult:
                info = detect_system()
                components = detect_components()
                plan = compute_install_plan(
                    info,
                    components,
                    create_venv=True,
                    install_kraken=True,
                    install_models=bool(selected_models),
                    models=selected_models,
                )
                installer = Installer(plan, info)
                return installer.run(interactive=False)

            def _done(result: InstallResult) -> None:
                self._inst_log.clear()
                for line in result.log:
                    self._inst_log.appendPlainText(line)
                if result.success:
                    self._set_t_label(
                        self._inst_status, "installer.installation_successful"
                    )
                    self._inst_progress.setValue(100)
                    self._btn_retry.setEnabled(False)
                else:
                    self._set_t_label(
                        self._inst_status,
                        "installer.installation_completed_with_errors",
                        count=len(result.errors),
                    )
                    self._btn_retry.setEnabled(True)
                    for err in result.errors:
                        self._inst_log.appendPlainText(
                            t("installer.error_prefix", error=err)
                        )
                self._btn_install.setEnabled(True)
                self._btn_check.setEnabled(True)

            def _error(message: str) -> None:
                self._set_t_label(
                    self._inst_status, "installer.installation_failed", message=message
                )
                self._inst_log.appendPlainText(
                    t("installer.error_prefix", error=message)
                )
                self._btn_install.setEnabled(True)
                self._btn_check.setEnabled(True)
                self._btn_retry.setEnabled(True)

            self._run_service(_install, _done, error_handler=_error)

        def _on_installer_retry(self) -> None:
            """Retry the installation."""
            self._on_installer_install()

        def closeEvent(self, event) -> None:  # type: ignore[override]
            """Ensure worker thread is finished before closing the window."""
            if self._worker_thread is not None and not self._worker_thread.isFinished():
                self._worker_thread.quit()
                if not self._worker_thread.wait(3000):
                    self._worker_thread.terminate()
                    self._worker_thread.wait(3000)
            super().closeEvent(event)

        # ------------------------------------------------------------------
        # Settings
        # ------------------------------------------------------------------
        def _settings_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            self._set_url = QLineEdit(self.service.base_url)
            self._set_book = QLineEdit(self.service.book)
            self._set_dir = QLineEdit(self.service.out_dir)
            self._set_settings_file = QLineEdit(self.service.settings_file)

            self._set_url_label = self._t_label("settings.base_url")
            layout.addWidget(self._set_url_label)
            layout.addWidget(self._set_url)
            self._set_book_label = self._t_label("settings.book_id")
            layout.addWidget(self._set_book_label)
            layout.addWidget(self._set_book)
            self._set_dir_label = self._t_label("settings.download_directory")
            layout.addWidget(self._set_dir_label)
            layout.addWidget(self._set_dir)
            self._set_settings_file_label = self._t_label("settings.settings_file")
            layout.addWidget(self._set_settings_file_label)
            layout.addWidget(self._set_settings_file)

            # Language selector
            lang_row = QHBoxLayout()
            self._lang_label = self._t_label("settings.language")
            self._lang_combo = QComboBox()
            for lang_code in get_available_languages():
                self._lang_combo.addItem(get_language_name(lang_code), lang_code)
            self._select_language_in_combo(translator.get_language())
            self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
            lang_row.addWidget(self._lang_label)
            lang_row.addWidget(self._lang_combo)
            lang_row.addStretch()
            layout.addLayout(lang_row)

            btn_row = QHBoxLayout()
            self._btn_save_set = QPushButton(t("settings.save"))
            self._btn_reload_set = QPushButton(t("settings.reload"))
            btn_row.addWidget(self._btn_save_set)
            btn_row.addWidget(self._btn_reload_set)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            self._set_result = QLabel("")
            self._set_result.setWordWrap(True)
            layout.addWidget(self._set_result)

            info = self._t_label("settings.info")
            info.setWordWrap(True)
            info.setObjectName("subtitle")
            layout.addWidget(info)

            self._btn_save_set.clicked.connect(self._on_settings_save)
            self._btn_reload_set.clicked.connect(self._on_settings_reload)

            return panel

        def _on_settings_save(self) -> None:
            self.service.base_url = self._set_url.text().strip()
            self.service.book = self._set_book.text().strip() or self.service.book
            self.service.out_dir = self._set_dir.text().strip() or self.service.out_dir
            self.service.settings_file = (
                self._set_settings_file.text().strip() or self.service.settings_file
            )
            # The combo displays native names; the stored value is the code.
            lang_code = self._lang_combo.currentData()
            if isinstance(lang_code, str):
                self.service.language = lang_code
            self.service.save_settings()
            self._update_context_panel()
            self._res_url.setText(self.service.base_url)
            self._res_book.setText(self.service.book)
            self._res_dir.setText(self.service.out_dir)
            self._set_t_label(self._set_result, "settings.saved")

        def _on_settings_reload(self) -> None:
            self.service._load_settings()
            self._set_url.setText(self.service.base_url)
            self._set_book.setText(self.service.book)
            self._set_dir.setText(self.service.out_dir)
            self._set_settings_file.setText(self.service.settings_file)
            translator.set_language(self.service.language)
            # Normalise an unsupported persisted code to the language actually used.
            self.service.language = translator.get_language()
            self._select_language_in_combo(self.service.language)
            self._update_context_panel()
            self._retranslate_ui()
            self._set_t_label(self._set_result, "settings.reloaded")

        def _on_language_changed(self, index: int) -> None:
            """Apply the language selected in the combo box and persist it."""
            lang_code = self._lang_combo.itemData(index)
            if not isinstance(lang_code, str) or lang_code == translator.get_language():
                return
            translator.set_language(lang_code)
            self.service.language = lang_code
            self._retranslate_ui()
            try:
                self.service.save_settings()
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    t("common.error_title"),
                    t("settings.language_save_failed", error=str(exc)),
                )

        # ------------------------------------------------------------------
        # Help
        # ------------------------------------------------------------------
        def _help_section(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setSpacing(12)

            help_text = QTextEdit()
            help_text.setReadOnly(True)
            help_text.setPlainText(t("help.text"))
            layout.addWidget(help_text)
            return panel

        # ------------------------------------------------------------------
        # Transkription korrigieren
        # ------------------------------------------------------------------
        def _transcription_correction_section(self) -> QWidget:
            """Build the 'Transkription korrigieren' section.

            Layout:
              • Left: image with clickable word overlays (from PAGE-XML / Kraken OCR)
              • Right: transcription text, word list, correction controls
            Corrections are stored via the service layer and applied to the
            full transcription by character offset (descending), so different
            word lengths never shift other positions.
            """
            panel = QWidget()
            panel.setObjectName("correctionPanel")
            layout = QHBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            # ------------------------------------------------------------------
            # Left: image with word overlays
            # ------------------------------------------------------------------
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(12, 12, 12, 12)
            left_layout.setSpacing(8)

            info_row = QHBoxLayout()
            self._corr_image_info = self._t_label(
                "transcription_correction.no_image_loaded"
            )
            self._corr_image_info.setObjectName("subtitle")
            info_row.addWidget(self._corr_image_info)
            info_row.addStretch()
            left_layout.addLayout(info_row)

            self._corr_image_scroll = QScrollArea()
            self._corr_image_scroll.setWidgetResizable(True)
            self._corr_image_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._corr_image_label = _CorrectionImageLabel()
            self._corr_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._corr_image_label.setObjectName("imageView")
            self._corr_image_scroll.setWidget(self._corr_image_label)
            left_layout.addWidget(self._corr_image_scroll, 1)

            # Image navigation
            img_nav_row = QHBoxLayout()
            self._corr_btn_prev = QPushButton(t("transcription_correction.previous"))
            self._corr_btn_next = QPushButton(t("transcription_correction.next"))
            img_nav_row.addWidget(self._corr_btn_prev)
            img_nav_row.addWidget(self._corr_btn_next)
            img_nav_row.addStretch()
            left_layout.addLayout(img_nav_row)

            splitter.addWidget(left_panel)

            # ------------------------------------------------------------------
            # Right: transcription + corrections
            # ------------------------------------------------------------------
            right_panel = QWidget()
            right_layout = QVBoxLayout(right_panel)
            right_layout.setContentsMargins(12, 12, 12, 12)
            right_layout.setSpacing(8)

            # Transcription text
            right_layout.addWidget(
                self._t_label("transcription_correction.transcription")
            )
            self._corr_transcription = QTextBrowser()
            self._corr_transcription.setObjectName("correctionTranscription")
            self._corr_transcription.setMinimumHeight(200)
            right_layout.addWidget(self._corr_transcription, 1)

            # Word list
            right_layout.addWidget(self._t_label("transcription_correction.ocr_words"))
            self._corr_word_list = QListWidget()
            self._corr_word_list.setMaximumHeight(150)
            self._corr_word_list.setAccessibleName(
                t("transcription_correction.label_words")
            )
            right_layout.addWidget(self._corr_word_list)

            # Correction controls
            ctrl_row = QHBoxLayout()
            self._corr_btn_correct = QPushButton(t("transcription_correction.correct"))
            self._corr_btn_correct.setEnabled(False)
            self._corr_btn_revert = QPushButton(t("transcription_correction.revert"))
            self._corr_btn_revert.setEnabled(False)
            self._corr_btn_refresh = QPushButton(t("transcription_correction.refresh"))
            ctrl_row.addWidget(self._corr_btn_correct)
            ctrl_row.addWidget(self._corr_btn_revert)
            ctrl_row.addWidget(self._corr_btn_refresh)
            ctrl_row.addStretch()
            right_layout.addLayout(ctrl_row)

            # Status
            self._corr_status = QLabel("")
            self._corr_status.setObjectName("subtitle")
            right_layout.addWidget(self._corr_status)

            splitter.addWidget(right_panel)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 1)
            layout.addWidget(splitter)

            # ------------------------------------------------------------------
            # State
            # ------------------------------------------------------------------
            self._corr_current_image_path: str | None = None
            self._corr_current_page_number: int | None = None
            self._corr_current_pixmap: QPixmap | None = None
            self._corr_words: list[dict[str, Any]] = []
            self._corr_selected_word: dict[str, Any] | None = None
            self._corr_word_overlays: list[_WordOverlay] = []

            # ------------------------------------------------------------------
            # Wire signals
            # ------------------------------------------------------------------
            self._corr_image_label.word_clicked.connect(self._on_corr_word_clicked)
            self._corr_btn_prev.clicked.connect(self._corr_prev_image)
            self._corr_btn_next.clicked.connect(self._corr_next_image)
            self._corr_word_list.currentRowChanged.connect(
                self._on_corr_word_list_row_changed
            )
            self._corr_btn_correct.clicked.connect(self._on_corr_correct)
            self._corr_btn_revert.clicked.connect(self._on_corr_revert)
            self._corr_btn_refresh.clicked.connect(self._corr_refresh)

            self._corr_refresh()
            return panel

        # ------------------------------------------------------------------
        # Transkription korrigieren — handlers
        # ------------------------------------------------------------------
        def _corr_refresh(self) -> None:
            """Refresh the image list and load the first image."""
            self._corr_word_list.clear()
            self._corr_transcription.clear()
            self._corr_image_label.clear_overlays()
            self._corr_image_label.setPixmap(QPixmap())
            self._corr_current_image_path = None
            self._corr_current_page_number = None
            self._corr_current_pixmap = None
            self._corr_words = []
            self._corr_selected_word = None
            self._corr_btn_correct.setEnabled(False)
            self._corr_btn_revert.setEnabled(False)
            self._corr_status.setText("")

            images = self.service.list_images()
            if not images:
                self._set_t_label(
                    self._corr_image_info,
                    "transcription_correction.no_images_available",
                )
                return
            self._corr_image_list = images
            self._load_corr_image(images[0]["path"], images[0]["page_number"])

        def _corr_prev_image(self) -> None:
            if not hasattr(self, "_corr_image_list") or not self._corr_image_list:
                return
            idx = (
                self._corr_current_image_index
                if hasattr(self, "_corr_current_image_index")
                else 0
            )
            idx = max(0, idx - 1)
            img = self._corr_image_list[idx]
            self._load_corr_image(img["path"], img["page_number"])

        def _corr_next_image(self) -> None:
            if not hasattr(self, "_corr_image_list") or not self._corr_image_list:
                return
            idx = (
                self._corr_current_image_index
                if hasattr(self, "_corr_current_image_index")
                else 0
            )
            idx = min(len(self._corr_image_list) - 1, idx + 1)
            img = self._corr_image_list[idx]
            self._load_corr_image(img["path"], img["page_number"])

        def _load_corr_image(self, path: str, page_number: int) -> None:
            """Load an image, parse its PAGE-XML words, and show overlays."""
            self._corr_current_image_path = path
            self._corr_current_page_number = page_number
            self._corr_current_image_index = self._corr_image_list.index(
                next(i for i in self._corr_image_list if i["path"] == path)
            )

            pixmap = QPixmap(path)
            if pixmap.isNull():
                self._set_t_label(
                    self._corr_image_info,
                    "transcription_correction.cannot_load_image",
                    path=path,
                )
                self._corr_image_label.setPixmap(QPixmap())
                self._corr_current_pixmap = None
                self._corr_status.setText("")
                return
            self._corr_current_pixmap = pixmap
            self._corr_image_label.setPixmap(pixmap)
            self._set_t_label(
                self._corr_image_info,
                "transcription_correction.page_info",
                page_number=page_number,
                width=pixmap.width(),
                height=pixmap.height(),
            )

            # Parse PAGE-XML words for this image
            pagexml_path = self.service.find_pagexml_for_image(path)
            if pagexml_path:
                self._corr_words = self.service.parse_kraken_words(pagexml_path)
            else:
                self._corr_words = []

            self._corr_image_label.add_word_overlays(self._corr_words)
            self._corr_word_list.clear()
            for word in self._corr_words:
                text = word.get("text", "")
                self._corr_word_list.addItem(
                    text or t("transcription_correction.empty")
                )

            self._show_corr_transcription()
            self._corr_btn_correct.setEnabled(bool(self._corr_selected_word))
            self._corr_btn_revert.setEnabled(
                any(c.status == "active" for c in self.service.list_corrections())
            )
            self._set_t_label(
                self._corr_status,
                "transcription_correction.word_overlays_loaded",
                count=len(self._corr_words),
            )

        def _on_corr_word_clicked(self, word: dict[str, Any]) -> None:
            """Handle a click on a word overlay in the image."""
            self._corr_selected_word = word
            # Highlight in word list
            text = word.get("text", "")
            for i in range(self._corr_word_list.count()):
                item = self._corr_word_list.item(i)
                if item.text() == text or item.text() == text + "…":
                    self._corr_word_list.setCurrentRow(i)
                    break
            self._corr_btn_correct.setEnabled(True)
            self._set_t_label(
                self._corr_status,
                "transcription_correction.word_selected",
                text=text,
                x=word.get("x", 0),
                y=word.get("y", 0),
            )

        def _on_corr_word_list_row_changed(self, row: int) -> None:
            """Handle selection change in the word list."""
            if 0 <= row < len(self._corr_words):
                self._corr_selected_word = self._corr_words[row]
                self._corr_btn_correct.setEnabled(True)
                text = self._corr_selected_word.get("text", "")
                self._set_t_label(
                    self._corr_status,
                    "transcription_correction.word_selected_simple",
                    text=text,
                )

        def _show_corr_transcription(self) -> None:
            """Display the full corrected transcription for the current page."""
            if self._corr_current_page_number is None:
                return
            # Build corrected transcription from all active corrections
            source_file = self._corr_current_image_path or ""
            corrections = self.service.list_corrections(
                source_file=source_file, status="active"
            )
            # Get the original transcription from the PAGE-XML
            pagexml_path = self.service.find_pagexml_for_image(
                self._corr_current_image_path or ""
            )
            if pagexml_path:
                original = self.service.extract_transcription_from_pagexml(pagexml_path)
            else:
                original = ""
            corrected = self.service.build_corrected_transcription(
                original, corrections
            )
            self._corr_transcription.setPlainText(corrected)

        def _on_corr_correct(self) -> None:
            """Open the correction dialog for the selected word."""
            if not self._corr_selected_word:
                return
            word = self._corr_selected_word
            dlg = _CorrectionDialog(
                word=word,
                source_file=self._corr_current_image_path or "",
                page_number=self._corr_current_page_number or 0,
                service=self.service,
                parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._show_corr_transcription()
                self._corr_btn_revert.setEnabled(True)
                self._set_t_label(
                    self._corr_status, "transcription_correction.correction_saved"
                )

        def _on_corr_revert(self) -> None:
            """Revert the most recent active correction."""
            corrections = self.service.list_corrections(status="active")
            if not corrections:
                self._set_t_label(
                    self._corr_status, "transcription_correction.no_active_corrections"
                )
                return
            # Revert the most recent one
            latest = corrections[-1]
            self.service.revert_correction(latest.id)
            self._show_corr_transcription()
            self._set_t_label(
                self._corr_status,
                "transcription_correction.correction_reverted",
                original_text=latest.original_text,
            )

        # ------------------------------------------------------------------
        # Worker thread helper
        # ------------------------------------------------------------------
        def _run_service(
            self, func, callback, *args, error_handler=None, **kwargs
        ) -> None:
            """Run a service method in a worker thread and call callback with the result."""
            if self._worker_thread is not None and not self._worker_thread.isFinished():
                QMessageBox.warning(
                    self, t("common.busy_title"), t("common.busy_message")
                )
                return
            self._worker_thread = QThread()
            self._worker = ServiceWorker(func, *args, **kwargs)
            self._worker.moveToThread(self._worker_thread)
            self._worker.finished.connect(callback)
            self._worker.finished.connect(self._on_worker_finished)
            if error_handler is not None:
                self._worker.error.connect(error_handler)
            else:
                self._worker.error.connect(self._on_worker_error)
            self._worker.error.connect(self._on_worker_finished)
            # Schedule thread deletion when it finishes (avoids "destroyed while running")
            self._worker_thread.finished.connect(self._worker_thread.deleteLater)
            self._worker_thread.started.connect(self._worker.run)
            self._worker_thread.start()

        def _on_worker_finished(self) -> None:
            if self._worker_thread is not None:
                self._worker_thread.quit()
                # Do not call wait() here: it deadlocks if called from the
                # worker thread itself.  Thread deletion is handled by
                # deleteLater connected to QThread.finished.

        def _on_worker_error(self, message: str) -> None:
            QMessageBox.critical(self, t("common.error_title"), message)

        # ------------------------------------------------------------------
        # Theme
        # ------------------------------------------------------------------
        def _set_theme(self) -> None:
            self.setStyleSheet(
                """
                /* Produktbezogene Kirchenbuch-Palette: warme Erdtöne */
                QWidget#root { background: #1a1410; color: #e8dcc8; }
                QFrame#navigation { background: #140f0c; border-right: 1px solid #3a2e20; }
                QFrame#contextPanel { background: #1e1610; border-left: 1px solid #3a2e20; }
                QLabel#brand { color: #c5a059; font-size: 18px; font-weight: 700; letter-spacing: 2px; }
                QLabel#eyebrow { color: #c5a059; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
                QLabel#pageTitle { color: #f0e6d2; font-size: 30px; font-weight: 700; }
                QLabel#subtitle { color: #a89070; font-size: 13px; }
                QLabel#researchResult { color: #d8c8b0; font-size: 13px; }
                QLabel#providerStatusNone { color: #a89070; font-size: 12px; }
                QLabel#providerStatusActive { color: #c5a059; font-size: 12px; font-weight: 600; }
                QComboBox {
                    background: #1e1610; border: 1px solid #3a2e20; color: #e8dcc8;
                    padding: 4px 8px; border-radius: 4px;
                }
                QComboBox:focus { border: 1px solid #c5a059; }
                QComboBox::drop-down { border: 0; width: 20px; }
                QListWidget#dashboardList { background: #1e1610; border: 1px solid #3a2e20; color: #e8dcc8; }
                QListWidget#dashboardList::item { padding: 10px; border-radius: 4px; }
                QListWidget#dashboardList::item:selected { background: #3a2e20; color: #c5a059; }
                QListWidget#dashboardList::item:alternate { background: #241c14; }
                QListWidget { background: #1e1610; border: 1px solid #3a2e20; color: #e8dcc8; }
                QListWidget::item { padding: 8px; border-radius: 4px; }
                QListWidget::item:selected { background: #3a2e20; color: #c5a059; }
                QTextEdit, QPlainTextEdit, QLineEdit {
                    background: #1e1610; border: 1px solid #3a2e20; color: #e8dcc8;
                }
                QTextEdit:focus, QPlainTextEdit:focus, QLineEdit:focus { border: 1px solid #c5a059; }
                QPushButton { background: #c5a059; border: 0; border-radius: 6px; color: #1a1410; padding: 8px 14px; font-weight: 600; }
                QPushButton:hover { background: #d8b870; }
                QPushButton:disabled { background: #3a2e20; color: #a89070; }
                QPushButton:focus { outline: 2px solid #c5a059; outline-offset: 2px; }
                QProgressBar { border: 1px solid #3a2e20; background: #1e1610; text-align: center; }
                QProgressBar::chunk { background: #c5a059; }
                QScrollArea { border: 0; background: #1e1610; }
                QLabel#imageView { background: #0a0806; border: 1px solid #3a2e20; }
                QSplitter::handle { background: #3a2e20; }
                QStatusBar { background: #100c08; color: #a89070; border-top: 1px solid #3a2e20; }
                """
            )

        # ------------------------------------------------------------------
        # Retranslation
        # ------------------------------------------------------------------
        def _retranslate_ui(self) -> None:
            """Retranslate all UI widgets for the current language."""
            # Labels registered at construction time that still show their
            # initial translation (dynamic results are handled by their refresh).
            for index, (label, key, kwargs, previous) in enumerate(self._i18n_labels):
                if label.text() != previous:
                    continue
                text = t(key, **kwargs)
                label.setText(text)
                self._i18n_labels[index] = (label, key, kwargs, text)

            # Window title
            self.setWindowTitle(t("window.title"))

            # Navigation sidebar
            for i, section_key in enumerate(_SECTION_KEYS):
                item = self.navigation.item(i)
                if item is not None:
                    item.setText(t(_SECTION_T_KEYS[section_key]))
            self.navigation.setAccessibleName(t("a11y.navigation"))
            self.statusBar().setAccessibleName(t("a11y.status_bar"))
            self._books_list.setAccessibleName(t("a11y.image_list"))
            self._sources_list.setAccessibleName(t("sources.list_name"))
            self._corr_word_list.setAccessibleName(
                t("transcription_correction.label_words")
            )

            # Page titles
            for section, page_title in self._page_titles.items():
                page_title.setText(t(_SECTION_T_KEYS[section]))

            # Context panel
            self._set_t_label(self._ctx_title, "context.title")
            self._set_t_label(self._ctx_version, "context.version", version=__version__)
            selected = self.service.get_selected_provider()
            if selected is None:
                self._set_t_label(self._ctx_provider, "context.provider_none")
            else:
                self._set_t_label(
                    self._ctx_provider,
                    "context.provider",
                    name=selected["name"],
                    status=selected["verification_status"],
                )
            self._set_t_label(
                self._ctx_book, "context.book", book=self.service.book or "—"
            )
            self._set_t_label(
                self._ctx_dir, "context.dir", directory=self.service.out_dir or "—"
            )
            self._set_t_label(self._ctx_status, "status.ready")
            self._set_t_label(
                self._ctx_ocr, "context.ocr", status=t("context.ocr_idle")
            )

            # Status bar
            self.statusBar().showMessage(t("status.ready"))

            # Dashboard
            for i in range(self._dashboard_list.count()):
                item = self._dashboard_list.item(i)
                if item is None:
                    continue
                label = item.data(Qt.ItemDataRole.UserRole)
                key = "dashboard." + label.lower().replace(" ", "_")
                count = item.text().split(": ")[-1] if ": " in item.text() else "0"
                item.setText(f"{t(key)}: {count}")
            self._set_t_label(self._dash_note, "dashboard.note")

            # Research section
            self._set_t_label(self._res_url_label, "research.base_url")
            self._set_t_label(self._res_book_label, "research.book_id")
            self._set_t_label(self._res_dir_label, "research.download_dir")
            self._set_t_label(self._res_provider_label, "research.provider")
            self._set_t_label(self._res_from_label, "research.from")
            self._btn_validate.setText(t("research.btn_validate"))
            self._btn_download_page.setText(t("research.btn_download_page"))
            self._btn_download_range.setText(t("research.btn_download_range"))
            self._btn_save_settings.setText(t("research.btn_save_settings"))
            self._set_t_label(
                self._res_provider_status, "research.provider_none_selected_full"
            )
            selected = self.service.get_selected_provider()
            if selected is not None:
                caps = []
                if selected["enabled"]:
                    caps.append(t("research.cap_enabled"))
                else:
                    caps.append(t("research.cap_disabled"))
                caps.append(
                    t("research.cap_verified", status=selected["verification_status"])
                )
                caps.append(
                    t("research.cap_image_access", access=selected["image_access"])
                )
                self._set_t_label(
                    self._res_provider_status,
                    "research.provider_status",
                    name=selected["name"],
                    caps=", ".join(caps),
                )
            self._refresh_provider_combo()

            # Books section
            self._btn_refresh_books.setText(t("books.refresh_list"))
            self._btn_verify_selected.setText(t("books.verify_selected"))

            # Image browser section
            self._btn_prev.setText(t("image_browser.previous"))
            self._btn_next.setText(t("image_browser.next"))
            self._btn_rotate_ccw.setText(t("image_browser.rotate_ccw"))
            self._btn_rotate_ccw.setToolTip(t("image_browser.rotate_ccw_tip"))
            self._btn_rotate_cw.setText(t("image_browser.rotate_cw"))
            self._btn_rotate_cw.setToolTip(t("image_browser.rotate_cw_tip"))
            self._btn_fit_window.setText(t("image_browser.fit_window"))
            self._btn_fit_width.setText(t("image_browser.fit_width"))
            self._btn_fullscreen.setText(t("image_browser.fullscreen"))
            self._btn_zoom_out.setText(t("image_browser.zoom_out"))
            self._btn_zoom_reset.setText(t("image_browser.zoom_reset"))
            self._btn_zoom_in.setText(t("image_browser.zoom_in"))

            # Hit checking section
            self._btn_show_diff.setText(t("hit_checking.show_diff"))
            self._btn_load_hit.setText(t("hit_checking.load_ocr_result"))
            self._btn_save_hit.setText(t("hit_checking.save_correction"))
            self._btn_export_hit.setText(t("hit_checking.export_correction"))

            # Sources section
            self._btn_save_source.setText(t("sources.save_metadata"))
            self._btn_refresh_sources.setText(t("sources.refresh"))
            self._btn_verify_all.setText(t("sources.verify_all"))
            self._btn_export_sources.setText(t("sources.export_json"))
            self._btn_export_sources_csv.setText(t("sources.export_csv"))

            # OCR jobs section
            self._btn_refresh_ocr.setText(t("ocr.refresh_inventory"))
            self._btn_run_ocr.setText(t("ocr.run_ocr_selected"))

            # System section
            self._btn_refresh_sys.setText(t("system.refresh_now"))

            # Installer section
            self._btn_check.setText(t("installer.check_system"))
            self._btn_install.setText(t("installer.install_missing"))
            self._btn_retry.setText(t("installer.retry_failed"))

            # Settings section
            self._set_t_label(self._set_url_label, "settings.base_url")
            self._set_t_label(self._set_book_label, "settings.book_id")
            self._set_t_label(self._set_dir_label, "settings.download_directory")
            self._set_t_label(self._set_settings_file_label, "settings.settings_file")
            self._btn_save_set.setText(t("settings.save"))
            self._btn_reload_set.setText(t("settings.reload"))
            # Language selector
            self._set_t_label(self._lang_label, "settings.language")
            self._select_language_in_combo(translator.get_language())

            # Help section

            # Transcription correction section
            self._corr_btn_prev.setText(t("transcription_correction.previous"))
            self._corr_btn_next.setText(t("transcription_correction.next"))
            self._corr_btn_correct.setText(t("transcription_correction.correct"))
            self._corr_btn_revert.setText(t("transcription_correction.revert"))
            self._corr_btn_refresh.setText(t("transcription_correction.refresh"))

            # Placeholder texts
            self._hit_original.setPlaceholderText(
                t("hit_checking.no_transcription_loaded")
            )
            self._hit_corrected.setPlaceholderText(t("hit_checking.enter_correction"))
            self._hit_diff.setPlaceholderText(t("hit_checking.diff_placeholder"))

            # Values rendered inline from live service state
            self._refresh_dashboard()
            self._refresh_system()


# ------------------------------------------------------------------
# Correction dialog (module level, outside MainWindow)
# ------------------------------------------------------------------

if QT_AVAILABLE:

    class _CorrectionDialog(QDialog):
        """Dialog for manually correcting a single OCR word."""

        def __init__(
            self,
            word: dict[str, Any],
            source_file: str,
            page_number: int,
            service: MatriculaDesktopService,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle(t("transcription_correction.dialog_title"))
            self.setModal(True)
            self.resize(500, 350)
            self._word = word
            self._source_file = source_file
            self._page_number = page_number
            self._service = service

            layout = QVBoxLayout(self)

            # Original text
            layout.addWidget(QLabel(t("transcription_correction.original_text")))
            self._orig_edit = QTextEdit()
            self._orig_edit.setPlainText(word.get("text", ""))
            self._orig_edit.setReadOnly(True)
            self._orig_edit.setMaximumHeight(60)
            layout.addWidget(self._orig_edit)

            # Corrected text
            layout.addWidget(QLabel(t("transcription_correction.corrected_text")))
            self._corr_edit = QTextEdit()
            self._corr_edit.setPlainText(word.get("text", ""))
            self._corr_edit.setMaximumHeight(80)
            layout.addWidget(self._corr_edit)

            # Position info
            pos_row = QHBoxLayout()
            pos_row.addWidget(
                QLabel(
                    t(
                        "transcription_correction.position",
                        x=word.get("x", 0),
                        y=word.get("y", 0),
                    )
                )
            )
            pos_row.addWidget(
                QLabel(
                    t(
                        "transcription_correction.size",
                        width=word.get("width", 0),
                        height=word.get("height", 0),
                    )
                )
            )
            pos_row.addStretch()
            layout.addLayout(pos_row)

            # Readability — the combo shows a translated label but stores the
            # technical status code, so the stored correction never depends on
            # the active UI language.
            read_row = QHBoxLayout()
            read_row.addWidget(QLabel(t("transcription_correction.readability")))
            self._readability = QComboBox()
            self._readability.addItem(
                t("transcription_correction.readability_sure"),
                ReadabilityStatus.SICHER,
            )
            self._readability.addItem(
                t("transcription_correction.readability_unsure"),
                ReadabilityStatus.UNSICHER,
            )
            self._readability.addItem(
                t("transcription_correction.readability_unreadable"),
                ReadabilityStatus.UNLESERLICH,
            )
            self._readability.setCurrentIndex(0)
            read_row.addWidget(self._readability)
            read_row.addStretch()
            layout.addLayout(read_row)

            # Image coordinates
            coord_row = QHBoxLayout()
            coord_row.addWidget(
                QLabel(t("transcription_correction.image_x", x=word.get("x", 0)))
            )
            coord_row.addWidget(
                QLabel(t("transcription_correction.image_y", y=word.get("y", 0)))
            )
            coord_row.addStretch()
            layout.addLayout(coord_row)

            # Buttons
            btn_box = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save
                | QDialogButtonBox.StandardButton.Cancel
            )
            btn_box.accepted.connect(self._save)
            btn_box.rejected.connect(self.reject)
            layout.addWidget(btn_box)

        def _save(self) -> None:
            corrected = self._corr_edit.toPlainText().strip()
            if not corrected:
                QMessageBox.warning(
                    self,
                    t("transcription_correction.error_title"),
                    t("transcription_correction.text_cannot_be_empty"),
                )
                return
            from matricula_toolkit.desktop_services import CorrectionRecord

            word = self._word
            readability = self._readability.currentData()
            correction = CorrectionRecord(
                id="",  # will be generated by the service
                source_file=self._source_file,
                page_number=self._page_number,
                book="",
                original_text=word.get("text", ""),
                corrected_text=corrected,
                original_text_position=word.get("position", 0),
                original_text_length=len(word.get("text", "")),
                image_x=word.get("x", 0),
                image_y=word.get("y", 0),
                image_width=word.get("width", 0),
                image_height=word.get("height", 0),
                image_width_px=0,
                image_height_px=0,
                line_number=word.get("line"),
                readability=(
                    readability
                    if isinstance(readability, str)
                    else ReadabilityStatus.SICHER
                ),
                status="active",
                created_at="",
                corrected_at=None,
                ocr_job_id=None,
                correction_type="word",
            )
            try:
                self._service.add_correction(correction)
                self.accept()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(
                    self, t("transcription_correction.error_title"), str(exc)
                )


def main(argv: list[str] | None = None) -> int:
    """Start the desktop app, or explain how to enable it when Qt is absent.

    Loads environment variables from a ``.env`` file in the current working
    directory (if present) before starting, so that ``MATRICULA_*`` variables
    defined there are picked up automatically.
    """
    load_dotenv()

    if not QT_AVAILABLE:
        print(
            "matricula-desktop requires PySide6. Install project dependencies with 'pip install .', then try again.",
            file=sys.stderr,
        )
        return 1
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


__all__ = ["SECTIONS", "MainWindow", "main"]
