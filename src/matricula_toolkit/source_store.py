"""Persistent SQLite storage for source records.

Provides a transaction-safe, schema-versioned SQLite store for
:class:`~matricula_toolkit.desktop_services.SourceRecord` objects. The store
lives in the platform-appropriate user data directory (XDG_DATA_HOME on
Linux, APPDATA on Windows, ~/Library/Application Support on macOS) so that
sources survive application restarts.

The store uses WAL journal mode and a busy timeout to tolerate concurrent
readers/writers, and a threading lock to serialise writes from within a
single process.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from matricula_toolkit.desktop_services import SourceRecord

# Schema version: bump when the table layout changes and write a migration.
SCHEMA_VERSION = 2

_STORE_NAME = "sources.db"


def _user_data_dir() -> Path:
    """Return the platform-appropriate user data directory for this app."""
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home())
        return Path(base) / "MatriculaToolkit"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MatriculaToolkit"
    # Linux and other Unix-like: follow XDG Base Directory Specification.
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "matricula-toolkit"


def default_db_path() -> Path:
    """Return the default path to the SQLite database file."""
    directory = _user_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / _STORE_NAME


# Zellwerte, die mit einem dieser Zeichen beginnen, werden von
# Tabellenkalkulationen (Excel, LibreOffice) als Formeln interpretiert.
# Ein führendes Apostroph kennzeichnet den Inhalt als Text und wird
# in der Anzeige nicht gezeigt.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t")


def _sanitize_csv_value(value: Any) -> Any:
    """Schützt einen Zellwert vor Formelinjektion in Tabellenkalkulationen.

    Werte, die mit ``=``, ``+``, ``-``, ``@`` oder einem Tabulator beginnen,
    erhalten ein führendes Apostroph, sodass sie als Text behandelt werden.
    Das bestehende CSV-Quoting von ``csv.DictWriter`` bleibt unverändert.
    """
    if isinstance(value, str) and value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


class SourceStoreError(Exception):
    """Raised when the source store cannot read or write its database."""


class SourceStore:
    """Transaction-safe SQLite store for :class:`SourceRecord` objects."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> SourceStore:  # noqa: PYI034
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema initialisation / migration
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        with self._lock:
            conn = self.conn
            # Create the meta table for schema version tracking.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'"
            ).fetchone()
            current_version = int(row["value"]) if row else 0

            if current_version < SCHEMA_VERSION:
                self._migrate(conn, current_version)
                conn.execute(
                    "INSERT OR REPLACE INTO store_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply schema migrations from *from_version* to SCHEMA_VERSION."""
        if from_version < 1:
            conn.execute(
                """
                CREATE TABLE sources (
                    id TEXT PRIMARY KEY,
                    archive TEXT NOT NULL DEFAULT '',
                    municipality TEXT NOT NULL DEFAULT '',
                    book TEXT NOT NULL DEFAULT '',
                    page INTEGER NOT NULL DEFAULT 0,
                    dating TEXT NOT NULL DEFAULT '',
                    signature TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'unverified',
                    file_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_book_page "
                "ON sources (book, page)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_verification "
                "ON sources (verification_status)"
            )

        if from_version < 2:
            conn.execute(
                """
                CREATE TABLE corrections (
                    id TEXT PRIMARY KEY,
                    source_file TEXT NOT NULL DEFAULT '',
                    page_number INTEGER NOT NULL DEFAULT 0,
                    book TEXT NOT NULL DEFAULT '',
                    original_text TEXT NOT NULL DEFAULT '',
                    corrected_text TEXT NOT NULL DEFAULT '',
                    original_text_position INTEGER NOT NULL DEFAULT 0,
                    original_text_length INTEGER NOT NULL DEFAULT 0,
                    image_x INTEGER NOT NULL DEFAULT 0,
                    image_y INTEGER NOT NULL DEFAULT 0,
                    image_width INTEGER NOT NULL DEFAULT 0,
                    image_height INTEGER NOT NULL DEFAULT 0,
                    image_width_px INTEGER NOT NULL DEFAULT 0,
                    image_height_px INTEGER NOT NULL DEFAULT 0,
                    line_number INTEGER,
                    readability TEXT NOT NULL DEFAULT 'SICHER',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    corrected_at TEXT,
                    ocr_job_id TEXT,
                    correction_type TEXT NOT NULL DEFAULT 'word'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_source_page "
                "ON corrections (source_file, page_number)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_status "
                "ON corrections (status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_position "
                "ON corrections (original_text_position)"
            )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def add(self, source: SourceRecord) -> SourceRecord:
        """Insert a new source record and return it with timestamps set."""

        now = datetime.now(timezone.utc).isoformat()
        if not source.created_at:
            source.created_at = now
        source.updated_at = now
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sources
                (id, archive, municipality, book, page, dating, signature,
                 notes, verification_status, file_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.archive,
                    source.municipality,
                    source.book,
                    source.page,
                    source.dating,
                    source.signature,
                    source.notes,
                    source.verification_status,
                    source.file_path,
                    source.created_at,
                    source.updated_at,
                ),
            )
            self.conn.commit()
        return source

    def get(self, source_id: str) -> SourceRecord | None:
        """Return a source by ID, or None if not found."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list(self) -> list[SourceRecord]:
        """Return all source records ordered by created_at."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM sources ORDER BY created_at"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_by_book_page(self, book: str, page: int) -> SourceRecord | None:
        """Return the first source matching book + page, or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sources WHERE book = ? AND page = ? LIMIT 1",
                (book, page),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, source_id: str, **fields: Any) -> SourceRecord | None:
        """Update fields on an existing source. Returns the updated record or None."""
        allowed = {
            "archive",
            "municipality",
            "book",
            "page",
            "dating",
            "signature",
            "notes",
            "verification_status",
            "file_path",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get(source_id)

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [source_id]

        with self._lock:
            cursor = self.conn.execute(
                f"UPDATE sources SET {set_clause} WHERE id = ?", values
            )
            self.conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get(source_id)

    def delete(self, source_id: str) -> bool:
        """Delete a source by ID. Returns True if a row was removed."""
        with self._lock:
            cursor = self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            self.conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Correction CRUD
    # ------------------------------------------------------------------
    def add_correction(self, correction: Any) -> Any:
        """Insert a new correction record and return it."""
        now = datetime.now(timezone.utc).isoformat()
        if not correction.created_at:
            correction.created_at = now
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO corrections
                (id, source_file, page_number, book, original_text,
                 corrected_text, original_text_position, original_text_length,
                 image_x, image_y, image_width, image_height,
                 image_width_px, image_height_px, line_number, readability,
                 status, created_at, corrected_at, ocr_job_id, correction_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction.id,
                    correction.source_file,
                    correction.page_number,
                    correction.book,
                    correction.original_text,
                    correction.corrected_text,
                    correction.original_text_position,
                    correction.original_text_length,
                    correction.image_x,
                    correction.image_y,
                    correction.image_width,
                    correction.image_height,
                    correction.image_width_px,
                    correction.image_height_px,
                    correction.line_number,
                    correction.readability,
                    correction.status,
                    correction.created_at,
                    correction.corrected_at,
                    correction.ocr_job_id,
                    correction.correction_type,
                ),
            )
            self.conn.commit()
        return correction

    def get_correction(self, correction_id: str) -> Any | None:
        """Return a correction by ID, or None if not found."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM corrections WHERE id = ?", (correction_id,)
            ).fetchone()
        return self._row_to_correction(row) if row else None

    def list_corrections(
        self,
        *,
        source_file: str | None = None,
        page_number: int | None = None,
        book: str | None = None,
        status: str | None = None,
    ) -> list[Any]:  # type: ignore[valid-type]  # 'list' shadows SourceStore.list method in class scope
        """Return corrections, optionally filtered."""
        conditions: list[str] = []
        values: list[Any] = []
        if source_file is not None:
            conditions.append("source_file = ?")
            values.append(source_file)
        if page_number is not None:
            conditions.append("page_number = ?")
            values.append(page_number)
        if book is not None:
            conditions.append("book = ?")
            values.append(book)
        if status is not None:
            conditions.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM corrections{where} ORDER BY created_at", values
            ).fetchall()
        return [self._row_to_correction(r) for r in rows]

    def update_correction(
        self,
        correction_id: str,
        *,
        corrected_text: str | None = None,
        readability: str | None = None,
        status: str | None = None,
        corrected_at: str | None = None,
    ) -> Any | None:
        """Update fields on an existing correction. Returns the updated record or None."""
        allowed: dict[str, Any] = {}
        if corrected_text is not None:
            allowed["corrected_text"] = corrected_text
        if readability is not None:
            allowed["readability"] = readability
        if status is not None:
            allowed["status"] = status
        if corrected_at is not None:
            allowed["corrected_at"] = corrected_at
        if not allowed:
            return self.get_correction(correction_id)

        set_clause = ", ".join(f"{k} = ?" for k in allowed)
        values = list(allowed.values()) + [correction_id]

        with self._lock:
            cursor = self.conn.execute(
                f"UPDATE corrections SET {set_clause} WHERE id = ?", values
            )
            self.conn.commit()
            if cursor.rowcount == 0:
                return None
        return self.get_correction(correction_id)

    def delete_correction(self, correction_id: str) -> bool:
        """Delete a correction by ID. Returns True if a row was removed."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM corrections WHERE id = ?", (correction_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_json(self) -> str:
        """Export all sources as a JSON string."""
        records = [asdict(r) for r in self.list()]
        return json.dumps(records, indent=2, ensure_ascii=False)

    def export_csv(self) -> str:
        """Export all sources as a CSV string."""
        records = [asdict(r) for r in self.list()]
        if not records:
            return ""
        fields = list(records[0].keys())
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        sanitized = [
            {k: _sanitize_csv_value(v) for k, v in row.items()} for row in records
        ]
        writer.writerows(sanitized)
        return buffer.getvalue()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SourceRecord:
        from matricula_toolkit.desktop_services import SourceRecord

        return SourceRecord(
            id=row["id"],
            archive=row["archive"],
            municipality=row["municipality"],
            book=row["book"],
            page=row["page"],
            dating=row["dating"],
            signature=row["signature"],
            notes=row["notes"],
            verification_status=row["verification_status"],
            file_path=row["file_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> Any:
        from matricula_toolkit.desktop_services import CorrectionRecord

        return CorrectionRecord(
            id=row["id"],
            source_file=row["source_file"],
            page_number=row["page_number"],
            book=row["book"],
            original_text=row["original_text"],
            corrected_text=row["corrected_text"],
            original_text_position=row["original_text_position"],
            original_text_length=row["original_text_length"],
            image_x=row["image_x"],
            image_y=row["image_y"],
            image_width=row["image_width"],
            image_height=row["image_height"],
            image_width_px=row["image_width_px"],
            image_height_px=row["image_height_px"],
            line_number=row["line_number"],
            readability=row["readability"],
            status=row["status"],
            created_at=row["created_at"],
            corrected_at=row["corrected_at"],
            ocr_job_id=row["ocr_job_id"],
            correction_type=row["correction_type"],
        )
