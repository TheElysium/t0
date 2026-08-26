"""Tests of :mod:`t0.ops.reconcile` -- PVC <-> ``raw.fit_file`` scan.

Minimal connection test double instead of ``FakePollDB``: this module runs only one
query, a SELECT on ``raw.fit_file``, and needs neither window machinery nor a clock.
No PostgreSQL, no network; files written to ``tmp_path``, never a real volume.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from t0.ops.reconcile import format_reconcile_report, reconcile_archive

if TYPE_CHECKING:
    from pathlib import Path

_CONTENT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        assert "raw.fit_file" in sql

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """Read-only connection: any write attempt makes the test fail."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.rows)


def _row(fit_file_id: int, path: Path, content: bytes = _CONTENT) -> tuple[Any, ...]:
    return (fit_file_id, 111, str(path), hashlib.sha256(content).digest(), len(content))


def _write(path: Path, content: bytes = _CONTENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_coherent_inventory_is_clean(tmp_path: Path) -> None:
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit")
    report = reconcile_archive(_FakeConn([_row(1, archived)]), tmp_path)

    assert report.missing_files == ()
    assert report.orphan_files == ()
    assert report.rows_scanned == 1
    assert report.is_clean is True


def test_row_without_file_is_critical_anomaly(tmp_path: Path) -> None:
    absent = tmp_path / "2026" / "08" / "activity-111-never-written.fit"
    report = reconcile_archive(_FakeConn([_row(1, absent)]), tmp_path)

    assert len(report.missing_files) == 1
    assert report.missing_files[0].fit_file_id == 1
    assert "missing" in report.missing_files[0].reason
    assert report.is_clean is False


def test_file_without_row_reported_but_remains_benign(tmp_path: Path) -> None:
    _write(tmp_path / "2026" / "08" / "activity-999-orphelin.fit")
    report = reconcile_archive(_FakeConn([]), tmp_path)

    assert len(report.orphan_files) == 1
    assert report.orphan_files[0].path.name == "activity-999-orphelin.fit"
    assert report.missing_files == ()
    assert report.is_clean is True


def test_size_mismatch_critical_without_rereading_content(tmp_path: Path) -> None:
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit", _CONTENT + b"tronque")
    report = reconcile_archive(_FakeConn([_row(1, archived)]), tmp_path)

    assert len(report.missing_files) == 1
    assert "size mismatch" in report.missing_files[0].reason
    assert report.is_clean is False


def test_fast_mode_misses_corruption_at_constant_size(tmp_path: Path) -> None:
    corrompu = bytearray(_CONTENT)
    corrompu[-1] = 0xFF
    archived = _write(tmp_path / "2026" / "08" / "activity-111-abc.fit", bytes(corrompu))
    conn = _FakeConn([_row(1, archived, _CONTENT)])

    assert reconcile_archive(conn, tmp_path).missing_files == ()

    deep = reconcile_archive(conn, tmp_path, deep=True)
    assert len(deep.missing_files) == 1
    assert "sha256 mismatch" in deep.missing_files[0].reason
    assert deep.is_clean is False


def test_files_outside_archive_extensions_ignored(tmp_path: Path) -> None:
    _write(tmp_path / "2026" / "08" / ".activity-111.tmp-1234")
    report = reconcile_archive(_FakeConn([]), tmp_path)

    assert report.orphan_files == ()


def test_nonexistent_volume_does_not_raise(tmp_path: Path) -> None:
    report = reconcile_archive(_FakeConn([]), tmp_path / "never-created")

    assert report.orphan_files == ()
    assert report.is_clean is True


def test_text_report_names_verdict(tmp_path: Path) -> None:
    absent = tmp_path / "2026" / "08" / "activity-111-absent.fit"
    _write(tmp_path / "2026" / "08" / "activity-999-orphelin.fit")
    rendered = format_reconcile_report(reconcile_archive(_FakeConn([_row(1, absent)]), tmp_path))

    assert "CRITICAL ANOMALY" in rendered
    assert "activity-999-orphelin.fit" in rendered
    assert "NO deletion" in rendered
