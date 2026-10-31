"""Tests for the manual transcription correction feature.

Covers the service layer (CRUD, conflict detection, transcription
reconstruction) and the GUI section (headless, offscreen).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from matricula_toolkit.desktop_services import (
    CorrectionConflictError,
    CorrectionRecord,
    MatriculaDesktopService,
    ReadabilityStatus,
)


@pytest.fixture(autouse=True)
def _isolate_desktop_state(tmp_path, monkeypatch):
    """Keep tests away from the user's real settings file and database."""
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "matricula_toolkit.desktop_services.default_db_path",
        lambda: tmp_path / "sources.db",
    )


# ---------------------------------------------------------------------------
# Service layer tests
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> MatriculaDesktopService:
    db_path = tmp_path / "test.db"
    return MatriculaDesktopService(
        base_url="", book="01519", out_dir=str(tmp_path), db_path=str(db_path)
    )


def _make_correction(**overrides) -> CorrectionRecord:
    data = {
        "source_file": "/tmp/img.jpg",
        "page_number": 1,
        "book": "01519",
        "original_text": "Johann",
        "corrected_text": "Johannes",
        "original_text_position": 10,
        "original_text_length": 6,
        "image_x": 100,
        "image_y": 200,
        "image_width": 50,
        "image_height": 30,
        "image_width_px": 800,
        "image_height_px": 600,
        "line_number": 1,
        "readability": ReadabilityStatus.SICHER,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corrected_at": None,
        "ocr_job_id": None,
        "correction_type": "word",
    }
    data.update(overrides)
    return CorrectionRecord(**data)


def test_add_and_get_correction(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    corr = _make_correction()
    result = service.add_correction(corr)
    assert result.id == corr.id
    fetched = service.get_correction(corr.id)
    assert fetched is not None
    assert fetched.original_text == "Johann"
    assert fetched.corrected_text == "Johannes"
    assert fetched.status == "active"


def test_list_corrections_filtered(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    c1 = _make_correction(source_file="/tmp/a.jpg", page_number=1, status="active")
    c2 = _make_correction(source_file="/tmp/b.jpg", page_number=2, status="reverted")
    service.add_correction(c1)
    service.add_correction(c2)

    all_corr = service.list_corrections()
    assert len(all_corr) == 2

    active = service.list_corrections(status="active")
    assert len(active) == 1
    assert active[0].source_file == "/tmp/a.jpg"

    by_page = service.list_corrections(page_number=2)
    assert len(by_page) == 1
    assert by_page[0].status == "reverted"


def test_update_correction(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    corr = _make_correction()
    service.add_correction(corr)

    updated = service.update_correction(
        corr.id,
        corrected_text="Johannes der Täufer",
        readability=ReadabilityStatus.UNSICHER,
    )
    assert updated is not None
    assert updated.corrected_text == "Johannes der Täufer"
    assert updated.readability == ReadabilityStatus.UNSICHER
    assert updated.corrected_at is not None


def test_delete_correction(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    corr = _make_correction()
    service.add_correction(corr)
    assert service.get_correction(corr.id) is not None
    assert service.delete_correction(corr.id) is True
    assert service.get_correction(corr.id) is None
    assert service.delete_correction(corr.id) is False


def test_revert_and_restore_correction(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    corr = _make_correction()
    service.add_correction(corr)

    reverted = service.revert_correction(corr.id)
    assert reverted is not None
    assert reverted.status == "reverted"

    # list_corrections with status filter
    active = service.list_corrections(status="active")
    assert len(active) == 0
    reverted_list = service.list_corrections(status="reverted")
    assert len(reverted_list) == 1

    restored = service.restore_correction(corr.id)
    assert restored is not None
    assert restored.status == "active"


def test_revert_nonexistent(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    assert service.revert_correction("nonexistent") is None


def test_build_corrected_transcription(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    original = "Der Johann und die Maria wohnen hier."

    c1 = _make_correction(
        original_text="Johann",
        corrected_text="Johannes",
        original_text_position=4,
        original_text_length=6,
    )
    c2 = _make_correction(
        original_text="Maria",
        corrected_text="Mariana",
        original_text_position=19,
        original_text_length=5,
    )
    service.add_correction(c1)
    service.add_correction(c2)

    corrected = service.build_corrected_transcription(original, [c1, c2])
    assert corrected == "Der Johannes und die Mariana wohnen hier."


def test_build_corrected_transcription_descending_offset(tmp_path: Path) -> None:
    """Corrections applied descending by offset so different word lengths don't shift."""
    service = _make_service(tmp_path)
    original = "ABC DEF GHI"

    # First correction changes length (3 -> 5 chars)
    c1 = _make_correction(
        original_text="ABC",
        corrected_text="XXXXX",
        original_text_position=0,
        original_text_length=3,
    )
    # Second correction at a later offset
    c2 = _make_correction(
        original_text="GHI",
        corrected_text="Z",
        original_text_position=8,
        original_text_length=3,
    )
    service.add_correction(c1)
    service.add_correction(c2)

    corrected = service.build_corrected_transcription(original, [c1, c2])
    # Descending: GHI at 8 replaced first (no shift), then ABC at 0 replaced
    assert corrected == "XXXXX DEF Z"


def test_check_correction_conflict(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    c1 = _make_correction(
        original_text="Johann",
        original_text_position=10,
        original_text_length=6,
    )
    service.add_correction(c1)

    # Overlapping correction should raise
    c2 = _make_correction(
        original_text="hans",
        original_text_position=12,
        original_text_length=4,
    )
    with pytest.raises(CorrectionConflictError):
        service.add_correction(c2)

    # Non-overlapping correction should work
    c3 = _make_correction(
        original_text="Maria",
        original_text_position=20,
        original_text_length=5,
    )
    service.add_correction(c3)  # should not raise


def test_parse_kraken_words(tmp_path: Path) -> None:
    pagexml = tmp_path / "page.xml"
    pagexml.write_text(
        """<?xml version="1.0"?>
        <PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
          <Page>
            <TextRegion>
              <TextLine>
                <Coords points="10,20 100,20 100,50 10,50"/>
                <Word>
                  <Coords points="10,20 60,20 60,50 10,50"/>
                  <TextEquiv><Unicode>Johann</Unicode></TextEquiv>
                </Word>
                <Word>
                  <Coords points="70,20 100,20 100,50 70,50"/>
                  <TextEquiv><Unicode>hans</Unicode></TextEquiv>
                </Word>
              </TextLine>
            </TextRegion>
          </Page>
        </PcGts>
        """,
        encoding="utf-8",
    )

    words = MatriculaDesktopService.parse_kraken_words(str(pagexml))
    assert len(words) == 2
    assert words[0]["text"] == "Johann"
    assert words[0]["x"] == 10
    assert words[0]["y"] == 20
    assert words[0]["width"] == 50
    assert words[0]["height"] == 30
    assert words[1]["text"] == "hans"


def test_parse_kraken_words_missing_file(tmp_path: Path) -> None:
    words = MatriculaDesktopService.parse_kraken_words(
        str(tmp_path / "nonexistent.xml")
    )
    assert words == []


def test_parse_kraken_words_no_coords(tmp_path: Path) -> None:
    pagexml = tmp_path / "page.xml"
    pagexml.write_text(
        """<?xml version="1.0"?>
        <PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15">
          <Page>
            <TextRegion>
              <TextLine>
                <Word>
                  <TextEquiv><Unicode>test</Unicode></TextEquiv>
                </Word>
              </TextLine>
            </TextRegion>
          </Page>
        </PcGts>
        """,
        encoding="utf-8",
    )
    words = MatriculaDesktopService.parse_kraken_words(str(pagexml))
    assert len(words) == 1
    assert words[0]["text"] == "test"
    assert words[0]["x"] == 0
    assert words[0]["y"] == 0
    assert words[0]["width"] == 0
    assert words[0]["height"] == 0


# ---------------------------------------------------------------------------
# GUI tests (headless, offscreen)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM") != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_correction_section_loads() -> None:
    """Verify the correction section can be constructed headless."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import MainWindow

    QApplication.instance() or QApplication([])
    window = MainWindow()
    # Navigate to the correction section
    idx = window.navigation.row(
        window.navigation.item(window.navigation.count() - 2)
    )  # "Transkription korrigieren" is second to last
    window.navigation.setCurrentRow(idx)
    window._corr_refresh()
    window.close()


@pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM") != "offscreen",
    reason="GUI tests require QT_QPA_PLATFORM=offscreen",
)
def test_correction_dialog(tmp_path: Path) -> None:
    """Verify the correction dialog can be constructed and accepted."""
    from PySide6.QtWidgets import QApplication

    from matricula_toolkit.desktop_app.app import _CorrectionDialog

    QApplication.instance() or QApplication([])
    service = _make_service(tmp_path)
    word = {
        "text": "Johann",
        "x": 10,
        "y": 20,
        "width": 50,
        "height": 30,
        "position": 10,
        "line": 1,
    }
    dlg = _CorrectionDialog(
        word=word,
        source_file="/tmp/img.jpg",
        page_number=1,
        service=service,
    )
    dlg._corr_edit.setPlainText("Johannes")
    dlg._save()
    # Should have added a correction
    corrections = service.list_corrections(status="active")
    assert len(corrections) == 1
    assert corrections[0].corrected_text == "Johannes"
    # The stored value is the technical status code, independent of the UI language.
    assert corrections[0].readability == ReadabilityStatus.SICHER
