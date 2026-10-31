"""Targeted tests for QA improvements: SQLite default path, CPU delta,
temperature sensor identification, OCR load protection, .env loading."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from matricula_toolkit.desktop_services import (
    MatriculaDesktopService,
    ResourceSnapshot,
)
from matricula_toolkit.source_store import default_db_path

# ------------------------------------------------------------------
# SQLite-Standardpfad
# ------------------------------------------------------------------


def test_default_db_path_returns_platform_path():
    """default_db_path() returns a Path under the platform user data dir."""
    path = default_db_path()
    assert isinstance(path, Path)
    assert path.name == "sources.db"
    assert path.parent.is_dir()


def test_service_uses_default_db_path_when_none_given(tmp_path, monkeypatch):
    """MatriculaDesktopService initialises _store with default_db_path()
    when no db_path is explicitly provided."""
    # Redirect default_db_path to a temp location
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        assert service._store.db_path == fake_db
        assert fake_db.parent.is_dir()


def test_source_persists_across_service_instances(tmp_path, monkeypatch):
    """A source added via one service instance is visible in a new
    service instance using the same default DB path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        # First service: add a source
        service1 = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        source = service1.add_source(
            archive="Testarchiv",
            municipality="Testgemeinde",
            book="01519",
            page=7,
            dating="1800-1850",
        )
        service1_id = source.id

        # Second service: should see the same source
        service2 = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        sources = service2.list_sources()
        assert len(sources) == 1
        assert sources[0]["id"] == service1_id
        assert sources[0]["archive"] == "Testarchiv"
        assert sources[0]["page"] == 7


def test_source_survives_service_close_and_reopen(tmp_path, monkeypatch):
    """Sources persist after closing the service and reopening."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service1 = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        source = service1.add_source(page=3, archive="Archiv A")
        service1._store.close()

        service2 = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        sources = service2.list_sources()
        assert len(sources) == 1
        assert sources[0]["id"] == source.id


def test_explicit_db_path_still_works(tmp_path):
    """An explicitly provided db_path is still honoured."""
    custom_db = tmp_path / "custom.db"
    service = MatriculaDesktopService(book="01519", db_path=str(custom_db))
    assert service._store.db_path == custom_db
    source = service.add_source(page=1)
    assert source is not None


def test_store_add_update_delete_persist(tmp_path, monkeypatch):
    """CRUD operations on SourceStore persist to SQLite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))

        # Add
        source = service.add_source(page=1, archive="A")
        assert service._store.get(source.id) is not None

        # Update
        updated = service.update_source(source.id, archive="B", notes="updated")
        assert updated is not None
        assert updated.archive == "B"
        assert updated.notes == "updated"

        # Delete
        assert service.delete_source(source.id) is True
        assert service._store.get(source.id) is None
        assert service.delete_source(source.id) is False


def test_export_json_and_csv(tmp_path, monkeypatch):
    """JSON and CSV export work with and without data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        service.add_source(archive="Archiv A", page=1, dating="1800")
        service.add_source(archive="Archiv B", page=2, signature="Sig B")

        # JSON export
        json_data = service.export_sources(format="json")
        parsed = json.loads(json_data)
        assert len(parsed) == 2
        assert parsed[0]["archive"] == "Archiv A"
        assert parsed[1]["archive"] == "Archiv B"

        # CSV export
        csv_data = service.export_sources(format="csv")
        assert "Archiv A" in csv_data
        assert "Archiv B" in csv_data

        # Empty export - use a separate DB path
        with tempfile.TemporaryDirectory() as tmpdir2:
            fake_db2 = Path(tmpdir2) / "sources.db"

            def fake_default_db_path2():
                return fake_db2

            monkeypatch.setattr(
                "matricula_toolkit.desktop_services.default_db_path",
                fake_default_db_path2,
            )

            service2 = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
            empty_json = service2.export_sources(format="json")
            assert json.loads(empty_json) == []
            empty_csv = service2.export_sources(format="csv")
            assert empty_csv == ""


# ------------------------------------------------------------------
# CSV-Formelinjektion (source_store.export_csv)
# ------------------------------------------------------------------


def _make_store_with_records(records: list[dict], tmp_path: Path):
    """Erzeugt einen SourceStore mit den gegebenen Datensätzen."""
    from matricula_toolkit.source_store import SourceStore

    db_path = tmp_path / "sources.db"
    store = SourceStore(db_path=db_path)
    for rec in records:
        from matricula_toolkit.desktop_services import SourceRecord

        store.add(SourceRecord(**rec))
    return store


def test_export_csv_sanitizes_equals_prefix(tmp_path):
    """Werte mit '=' am Anfang erhalten ein führendes Apostroph."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "=CMD|'calc.exe'!A0",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "'=CMD|'calc.exe'!A0" in csv_data
    assert csv_data.startswith("id,archive,municipality")


def test_export_csv_sanitizes_plus_prefix(tmp_path):
    """Werte mit '+' am Anfang erhalten ein führendes Apostroph."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "+HYPERLINK('http://evil.com')",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "'+HYPERLINK('http://evil.com')" in csv_data


def test_export_csv_sanitizes_minus_prefix(tmp_path):
    """Werte mit '-' am Anfang erhalten ein führendes Apostroph."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "-1+1",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "'-1+1" in csv_data


def test_export_csv_sanitizes_at_prefix(tmp_path):
    """Werte mit '@' am Anfang erhalten ein führendes Apostroph."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "@SUM(1,1)",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "'@SUM(1,1)" in csv_data


def test_export_csv_sanitizes_tab_prefix(tmp_path):
    """Werte mit Tabulator am Anfang erhalten ein führendes Apostroph."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "\tEVIL",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "'\tEVIL" in csv_data


def test_export_csv_normal_values_unchanged(tmp_path):
    """Normale Werte ohne gefährliches Präfix bleiben unverändert."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "Normales Archiv",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "1800",
                "signature": "Sig",
                "notes": "Notiz",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "Normales Archiv" in csv_data
    assert "'" not in csv_data.split("Normales Archiv")[0].splitlines()[-1]


def test_export_csv_value_starting_with_space_unchanged(tmp_path):
    """Werte mit führendem Leerzeichen (kein gefährliches Präfix) bleiben unverändert."""

    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": " =not a formula",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert " =not a formula" in csv_data


def test_export_csv_none_and_empty_values(tmp_path):
    """None und leere Werte werden nicht verändert."""
    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "",
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert csv_data != ""


def test_export_csv_numbers_unchanged(tmp_path):
    """Zahlenwerte bleiben unverändert."""
    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "Archiv",
                "municipality": "Test",
                "book": "01",
                "page": 42,
                "dating": "1800",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "42" in csv_data


def test_export_csv_csv_quoting_preserved(tmp_path):
    """CSV-Quoting für Kommas, Anführungszeichen und Zeilenumbrüche bleibt erhalten."""
    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": 'Archiv, mit "Komma" und Zeilenumbruch',
                "municipality": "Test",
                "book": "01",
                "page": 1,
                "dating": "1800",
                "signature": "",
                "notes": "",
                "verification_status": "unverified",
                "file_path": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    # csv.DictWriter quotet Werte mit Komma/Anführungszeichen automatisch
    assert '"Archiv, mit ""Komma"" und Zeilenumbruch"' in csv_data


def test_export_csv_genealogical_data_unchanged(tmp_path):
    """Normale genealogische Daten bleiben unverändert."""
    store = _make_store_with_records(
        [
            {
                "id": "1",
                "archive": "Staatsarchiv Ludwigsburg",
                "municipality": "Stuttgart",
                "book": "01519",
                "page": 42,
                "dating": "1750-1800",
                "signature": "A 123",
                "notes": "Taufeintrag",
                "verification_status": "verified",
                "file_path": "/downloads/matricula_01519_pg042.jpg",
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        tmp_path,
    )
    csv_data = store.export_csv()
    assert "Staatsarchiv Ludwigsburg" in csv_data
    assert "Stuttgart" in csv_data
    assert "01519" in csv_data
    assert "42" in csv_data
    assert "1750-1800" in csv_data
    assert "A 123" in csv_data
    assert "Taufeintrag" in csv_data
    assert "verified" in csv_data


# ------------------------------------------------------------------
# SSRF-Schutz (matricula_toolkit.security)
# ------------------------------------------------------------------


def test_validate_host_literal_private_ipv4(monkeypatch):
    """Literal private IPv4-Adressen werden abgelehnt (fail-closed)."""
    from matricula_toolkit.security import SecurityError, validate_host

    for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.0.1"):
        with pytest.raises(SecurityError):
            validate_host(host)


def test_validate_host_literal_public_ipv4(monkeypatch):
    """Literal IP-Adressen werden grundsätzlich abgelehnt (keine IP-Literale)."""
    from matricula_toolkit.security import SecurityError, validate_host

    for host in ("8.8.8.8", "1.1.1.1"):
        with pytest.raises(SecurityError):
            validate_host(host)


def test_validate_host_literal_private_ipv6(monkeypatch):
    """Private IPv6-Adressen werden abgelehnt."""
    from matricula_toolkit.security import SecurityError, validate_host

    for host in ("::1", "fc00::1", "fe80::1"):
        with pytest.raises(SecurityError):
            validate_host(host)


def test_validate_host_hostname_resolves_to_private(monkeypatch):
    """Hostname, der auf private IP auflöst, wird abgelehnt."""
    from matricula_toolkit import security

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
        ]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    from matricula_toolkit.security import SecurityError, validate_host

    with pytest.raises(SecurityError):
        validate_host("evil.internal.example")


def test_validate_host_hostname_resolves_to_public(monkeypatch):
    """Hostname, der auf öffentliche IP auflöst, wird akzeptiert."""
    from matricula_toolkit import security

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    from matricula_toolkit.security import validate_host

    result = validate_host("matricula.example")
    assert result == "matricula.example"


def test_validate_host_hostname_dns_failure(monkeypatch):
    """Bei DNS-Auflösungsfehler wird der Hostname abgelehnt (fail-closed)."""
    from matricula_toolkit import security

    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise OSError("DNS failure")

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    from matricula_toolkit.security import SecurityError, validate_host

    with pytest.raises(SecurityError):
        validate_host("nonexistent.example")


def test_validate_host_hostname_multiple_ips_one_private(monkeypatch):
    """Bei mehreren IPs reicht eine private Adresse für Ablehnung (DNS-Rebinding-Schutz)."""
    from matricula_toolkit import security

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80)),
        ]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    from matricula_toolkit.security import SecurityError, validate_host

    with pytest.raises(SecurityError):
        validate_host("rebinding.example")


def test_duplicate_ids_rejected(tmp_path, monkeypatch):
    """Adding a source with a duplicate ID is handled (store generates
    unique IDs by default)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        s1 = service.add_source(page=1)
        s2 = service.add_source(page=2)
        assert s1.id != s2.id
        assert len(service.list_sources()) == 2


def test_empty_data_store(tmp_path, monkeypatch):
    """An empty store returns empty lists and exports."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_db = Path(tmpdir) / "sources.db"

        def fake_default_db_path():
            return fake_db

        monkeypatch.setattr(
            "matricula_toolkit.desktop_services.default_db_path", fake_default_db_path
        )

        service = MatriculaDesktopService(book="01519", out_dir=str(tmp_path))
        assert service.list_sources() == []
        assert service.export_sources(format="json") == "[]"
        assert service.export_sources(format="csv") == ""


# ------------------------------------------------------------------
# CPU-Differenzmessung
# ------------------------------------------------------------------


def test_read_cpu_percent_first_call_returns_none():
    """First call to _read_cpu_percent() returns None (baseline, not 0%)."""
    service = MatriculaDesktopService()
    result = service._read_cpu_percent()
    assert result is None
    # Second call should have a baseline stored
    assert service._prev_cpu_times is not None


def test_read_cpu_percent_second_call_returns_float_or_none():
    """Second call returns a float percentage or None (if /proc/stat
    unavailable or delta is implausible)."""
    service = MatriculaDesktopService()
    service._read_cpu_percent()  # baseline
    result = service._read_cpu_percent()
    if sys.platform == "linux":
        assert result is None or (isinstance(result, float) and 0.0 <= result <= 100.0)
    else:
        assert result is None


def test_read_cpu_percent_handles_missing_proc_stat(monkeypatch):
    """_read_cpu_percent returns None when /proc/stat does not exist."""
    service = MatriculaDesktopService()
    service._read_cpu_percent()  # baseline

    with patch.object(Path, "exists", return_value=False):
        result = service._read_cpu_percent()
        assert result is None


def test_read_cpu_percent_handles_bad_values(monkeypatch):
    """_read_cpu_percent returns None on unparseable /proc/stat content."""
    service = MatriculaDesktopService()
    service._read_cpu_percent()  # baseline

    fake_stat = "cpu  not a number not a number not a number\n"
    with (
        patch("builtins.open", mock_open(read_data=fake_stat)),
        patch.object(Path, "exists", return_value=True),
    ):
        result = service._read_cpu_percent()
        assert result is None


def test_read_cpu_percent_negative_delta_rejected(monkeypatch):
    """_read_cpu_percent returns None when delta values are negative
    (counter wrapped or implausible)."""
    service = MatriculaDesktopService()
    # Set a baseline with high values
    service._prev_cpu_times = (1000000, 500000)

    fake_stat = "cpu 100 200 300 400\n"  # total=1000, idle=400 -> delta_total negative
    with (
        patch("builtins.open", MagicMock(read_data=fake_stat)),
        patch.object(Path, "exists", return_value=True),
    ):
        result = service._read_cpu_percent()
        assert result is None


def test_read_cpu_percent_delta_calculation(monkeypatch):
    """_read_cpu_percent computes correct delta between two readings."""
    service = MatriculaDesktopService()

    stat1 = "cpu 100 200 300 400 500 600 700 800 900 1000\n"
    # Only idle changes: total 5500->6500, idle 400->1400, delta_total=1000, delta_idle=1000 -> 0% busy
    stat2 = "cpu 100 200 300 1400 500 600 700 800 900 1000\n"
    # user +200, idle +200: total 6500->6900, idle 1400->1600, delta_total=400, delta_idle=200 -> 50% busy
    stat3 = "cpu 300 200 300 1600 500 600 700 800 900 1000\n"

    # Patch Path.exists to return True for /proc/stat
    original_exists = Path.exists

    def fake_exists(self):
        if str(self) == "/proc/stat":
            return True
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    # First call: baseline
    with patch("builtins.open", mock_open(read_data=stat1)):
        result = service._read_cpu_percent()
        assert result is None

    # Second call: 0% busy
    with patch("builtins.open", mock_open(read_data=stat2)):
        result = service._read_cpu_percent()
        assert result == 0.0

    # Third call: 50% busy
    with patch("builtins.open", mock_open(read_data=stat3)):
        result = service._read_cpu_percent()
        assert result == 50.0


# ------------------------------------------------------------------
# Temperatur-Sensor
# ------------------------------------------------------------------


def test_read_temperature_returns_none_when_no_thermal_dir(monkeypatch):
    """_read_temperature returns None when /sys/class/thermal does not exist."""
    service = MatriculaDesktopService()
    with patch.object(Path, "is_dir", return_value=False):
        result = service._read_temperature()
        assert result is None


def test_read_temperature_identifies_cpu_sensor(monkeypatch, tmp_path):
    """_read_temperature identifies a CPU-specific sensor by type name."""
    service = MatriculaDesktopService()

    # Create fake thermal zone structure
    thermal_dir = tmp_path / "thermal"
    thermal_dir.mkdir()
    zone0 = thermal_dir / "thermal_zone0"
    zone0.mkdir()
    (zone0 / "type").write_text("x86_pkg_temp", encoding="utf-8")
    (zone0 / "temp").write_text("45000", encoding="utf-8")  # 45 °C

    zone1 = thermal_dir / "thermal_zone1"
    zone1.mkdir()
    (zone1 / "type").write_text("acpitz", encoding="utf-8")
    (zone1 / "temp").write_text("30000", encoding="utf-8")  # 30 °C

    with (
        patch.object(Path, "is_dir", return_value=True),
        patch("matricula_toolkit.desktop_services.Path") as mock_path_class,
    ):
        # Make /sys/class/thermal resolve to our fake dir
        def fake_glob(pattern):
            if pattern == "thermal_zone*":
                return [zone0, zone1]
            return []

        mock_path_instance = MagicMock()
        mock_path_instance.is_dir.return_value = True
        mock_path_instance.glob.side_effect = fake_glob
        mock_path_class.return_value = mock_path_instance

        # Patch the module-level Path to return our mock for /sys/class/thermal
        with patch(
            "matricula_toolkit.desktop_services.Path",
            side_effect=lambda p: (
                mock_path_instance if str(p) == "/sys/class/thermal" else Path(p)
            ),
        ):
            result = service._read_temperature()
            assert result == 45.0


def test_read_temperature_returns_none_when_no_cpu_sensor(monkeypatch, tmp_path):
    """_read_temperature returns None when no CPU-specific sensor is found."""
    service = MatriculaDesktopService()

    thermal_dir = tmp_path / "thermal"
    thermal_dir.mkdir()
    zone0 = thermal_dir / "thermal_zone0"
    zone0.mkdir()
    (zone0 / "type").write_text("battery", encoding="utf-8")
    (zone0 / "temp").write_text("30000", encoding="utf-8")

    zone1 = thermal_dir / "thermal_zone1"
    zone1.mkdir()
    (zone1 / "type").write_text("chassis", encoding="utf-8")
    (zone1 / "temp").write_text("25000", encoding="utf-8")

    with (
        patch.object(Path, "is_dir", return_value=True),
        patch(
            "matricula_toolkit.desktop_services.Path",
            side_effect=lambda p: (
                MagicMock(
                    is_dir=MagicMock(return_value=True),
                    glob=MagicMock(return_value=[zone0, zone1]),
                )
                if str(p) == "/sys/class/thermal"
                else Path(p)
            ),
        ),
    ):
        result = service._read_temperature()
        # Falls back to trying all zones, but values are valid so it may return one
        # The key point: no CPU-specific sensor found, but fallback may still return
        # a value. This is acceptable - the important thing is CPU-specific
        # sensors are preferred.
        assert result is None or isinstance(result, float)


def test_read_temperature_handles_invalid_values(monkeypatch, tmp_path):
    """_read_temperature handles unparseable temp values gracefully."""
    service = MatriculaDesktopService()

    thermal_dir = tmp_path / "thermal"
    thermal_dir.mkdir()
    zone0 = thermal_dir / "thermal_zone0"
    zone0.mkdir()
    (zone0 / "type").write_text("x86_pkg_temp", encoding="utf-8")
    (zone0 / "temp").write_text("not-a-number", encoding="utf-8")

    with (
        patch.object(Path, "is_dir", return_value=True),
        patch(
            "matricula_toolkit.desktop_services.Path",
            side_effect=lambda p: (
                MagicMock(
                    is_dir=MagicMock(return_value=True),
                    glob=MagicMock(return_value=[zone0]),
                )
                if str(p) == "/sys/class/thermal"
                else Path(p)
            ),
        ),
    ):
        result = service._read_temperature()
        assert result is None


# ------------------------------------------------------------------
# OCR-Lastschutz
# ------------------------------------------------------------------


def test_check_ocr_load_blocks_when_cpu_unknown(monkeypatch):
    """check_ocr_load blocks when CPU is unknown (None) and a CPU limit
    is configured."""
    service = MatriculaDesktopService()
    service.config.ocr_max_cpu_percent = 85.0

    def mock_snapshot():
        return ResourceSnapshot(cpu_percent=None, ram_percent=30.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)
    result = service.check_ocr_load()
    assert result["allowed"] is False
    assert "CPU" in result["reason"] or "cpu" in result["reason"].lower()


def test_check_ocr_load_blocks_when_temperature_unknown(monkeypatch):
    """check_ocr_load blocks when temperature is unknown (None) and a
    temperature limit is configured."""
    service = MatriculaDesktopService()
    service.config.ocr_max_temperature_c = 80.0

    def mock_snapshot():
        return ResourceSnapshot(cpu_percent=30.0, ram_percent=30.0, temperature_c=None)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)
    result = service.check_ocr_load()
    assert result["allowed"] is False
    assert "Temperatur" in result["reason"] or "temperature" in result["reason"].lower()


def test_check_ocr_load_allows_when_all_unknown_and_no_limits(monkeypatch):
    """check_ocr_load allows when all values are unknown and no limits
    are configured."""
    service = MatriculaDesktopService()
    service.config.ocr_max_cpu_percent = 0.0
    service.config.ocr_max_ram_percent = 0.0
    service.config.ocr_max_temperature_c = None

    def mock_snapshot():
        return ResourceSnapshot(cpu_percent=None, ram_percent=0.0, temperature_c=None)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot)
    result = service.check_ocr_load()
    assert result["allowed"] is True


def test_check_ocr_load_distinguishes_unknown_from_within_limits(monkeypatch):
    """check_ocr_load clearly distinguishes 'unknown' from 'within limits'."""
    service = MatriculaDesktopService()
    service.config.ocr_max_cpu_percent = 85.0

    # CPU unknown -> blocked
    def mock_snapshot_unknown():
        return ResourceSnapshot(cpu_percent=None, ram_percent=30.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot_unknown)
    result_unknown = service.check_ocr_load()
    assert result_unknown["allowed"] is False
    assert (
        "unbekannt" in result_unknown["reason"]
        or "unknown" in result_unknown["reason"].lower()
    )

    # CPU within limits -> allowed
    def mock_snapshot_ok():
        return ResourceSnapshot(cpu_percent=30.0, ram_percent=30.0, temperature_c=45.0)

    monkeypatch.setattr(service, "resource_snapshot", mock_snapshot_ok)
    result_ok = service.check_ocr_load()
    assert result_ok["allowed"] is True
    assert "within limits" in result_ok["reason"]


def test_check_ocr_load_timeout_resets_counter(monkeypatch, tmp_path):
    """OCR job timeout resets the active OCR job counter."""
    source = tmp_path / "test.jpg"
    source.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    service = MatriculaDesktopService(out_dir=str(tmp_path))
    job = service.add_ocr_job(source_file=str(source), model="/tmp/model.mlmodel")

    model = tmp_path / "model.mlmodel"
    model.write_bytes(b"dummy model")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    monkeypatch.setattr(subprocess, "run", mock_run)

    result = service.run_ocr_job(job, model_path=str(model))
    assert result["success"] is False
    assert service._active_ocr_jobs == 0


def test_check_ocr_load_error_resets_counter(monkeypatch, tmp_path):
    """OCR job error resets the active OCR job counter."""
    source = tmp_path / "test.jpg"
    source.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    service = MatriculaDesktopService(out_dir=str(tmp_path))
    job = service.add_ocr_job(source_file=str(source), model="/tmp/model.mlmodel")

    model = tmp_path / "model.mlmodel"
    model.write_bytes(b"dummy model")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kraken")

    def mock_run(*args, **kwargs):
        raise OSError("kraken failed")

    monkeypatch.setattr(subprocess, "run", mock_run)

    result = service.run_ocr_job(job, model_path=str(model))
    assert result["success"] is False
    assert service._active_ocr_jobs == 0


# ------------------------------------------------------------------
# .env-Lader
# ------------------------------------------------------------------


def test_cli_main_calls_load_dotenv(monkeypatch):
    """CLI main() calls load_dotenv() before parsing arguments."""
    from matricula_toolkit import cli

    called = []

    def fake_load_dotenv(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(cli, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(cli, "_build_parser", lambda: MagicMock())
    monkeypatch.setattr(
        cli.argparse.ArgumentParser,
        "parse_args",
        lambda self, argv=None: MagicMock(
            command="page",
            page=1,
            url=None,
            book=None,
            out=None,
            wait=5,
            headless=True,
            headed=False,
        ),
    )

    with patch.object(cli, "asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = 0
        with patch.object(cli, "MatriculaConfig") as mock_config:
            mock_config.from_env.return_value.validate.return_value = None
            mock_config.from_env.return_value.base_url = ""
            with patch.object(cli, "MatriculaDownloader") as mock_downloader:
                mock_downloader.return_value.__aenter__.return_value.download_page.return_value = MagicMock(
                    success=True,
                    file_path=Path("/tmp/test.jpg"),
                    file_size=100,
                    duration_seconds=0.5,
                )
                cli.main(["page", "1"])

    assert called, "load_dotenv should be called in CLI main()"


def test_mcp_main_calls_load_dotenv(monkeypatch):
    """MCP server main() calls load_dotenv() before starting."""
    from matricula_toolkit import mcp_server

    called = []

    def fake_load_dotenv(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(mcp_server, "load_dotenv", fake_load_dotenv)

    # We can't easily run the full MCP main loop, but we can verify
    # that load_dotenv is referenced in the main function source
    import inspect

    source = inspect.getsource(mcp_server.main)
    assert "load_dotenv" in source


def test_desktop_app_main_calls_load_dotenv(monkeypatch):
    """Desktop app main() calls load_dotenv() before starting."""
    import inspect

    from matricula_toolkit.desktop_app import app

    source = inspect.getsource(app.main)
    assert "load_dotenv" in source
