"""Tests for :func:`garminator.garmin.raw_store.insert_fit_file` (batch L12).

``tests/garmin/fakes.py`` (existing, unmodified — another batch may depend on it) only
simulates query patterns from earlier batches (``RETURNING id`` for ``raw.api_response``,
``RETURNING ingest_run_id``). ``raw.fit_file`` has a different pattern
(``ON CONFLICT ... DO NOTHING RETURNING fit_file_id``, plus optional activity-aware
re-read query), so this file defines its own minimal double, dedicated to this module,
rather than modifying a double shared with other batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from garminator.garmin.raw_store import FitFileShaConflictError, insert_fit_file


@dataclass
class _Row:
    fit_file_id: int
    garmin_activity_id: int
    storage_path: str
    sha256: bytes
    size_bytes: int


class _FakeCursor:
    def __init__(self, conn: _FakeFitFileConnection) -> None:
        self._conn = conn
        self._result: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))

        if normalized.startswith("INSERT INTO raw.fit_file"):
            garmin_activity_id, storage_path, sha256, size_bytes = params
            existing = next((row for row in self._conn.rows if row.sha256 == sha256), None)
            if existing is not None:
                # ON CONFLICT (sha256) DO NOTHING: no "RETURNING" row in real SQL.
                self._result = None
                return
            self._conn.next_id += 1
            row = _Row(
                fit_file_id=self._conn.next_id,
                garmin_activity_id=garmin_activity_id,
                storage_path=storage_path,
                sha256=sha256,
                size_bytes=size_bytes,
            )
            self._conn.rows.append(row)
            self._result = (row.fit_file_id,)
            return

        if normalized.startswith("SELECT fit_file_id, garmin_activity_id FROM raw.fit_file"):
            (sha256,) = params
            existing = next((row for row in self._conn.rows if row.sha256 == sha256), None)
            self._result = (
                (existing.fit_file_id, existing.garmin_activity_id)
                if existing is not None
                else None
            )
            return

        raise AssertionError(f"_FakeCursor: unexpected query -> {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class _FakeFitFileConnection:
    """Satisfies ``garminator.garmin.raw_store.DBConnection``, in-memory."""

    rows: list[_Row] = field(default_factory=list)
    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    next_id: int = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


_SHA_A = bytes(range(32))
_SHA_B = bytes(range(1, 33))


def test_insert_fit_file_writes_expected_columns_and_commits() -> None:
    conn = _FakeFitFileConnection()

    fit_file_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    assert fit_file_id == 1
    assert conn.commit_count == 1
    # One query occurred (no conflict): INSERT was enough, re-read not triggered.
    # Parameter values matter more than exact SQL text — SQL reformulation must not
    # break this test (see review).
    assert len(conn.executed) == 1
    _, params = conn.executed[0]
    garmin_activity_id, storage_path, sha256, size_bytes = params
    assert garmin_activity_id == 12345
    assert storage_path == "/data/fit/2026/08/activity-12345.fit"
    assert sha256 == _SHA_A
    assert size_bytes == 4096


def test_insert_fit_file_same_sha256_same_activity_is_idempotent() -> None:
    conn = _FakeFitFileConnection()
    first_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    # Re-download of same activity: same content, same sha256. Must neither explode
    # (fit_file_sha_uq violation) nor create a second row.
    second_id = insert_fit_file(
        conn,
        garmin_activity_id=12345,
        storage_path="/data/fit/2026/08/activity-12345.fit",
        sha256=_SHA_A,
        size_bytes=4096,
    )

    assert second_id == first_id
    assert len(conn.rows) == 1
    # Second query (SELECT re-read after conflict) did occur and must read
    # garmin_activity_id to compare — not just fit_file_id.
    assert conn.executed[-1][0].startswith("SELECT fit_file_id, garmin_activity_id")


def test_insert_fit_file_different_sha256_creates_distinct_rows() -> None:
    conn = _FakeFitFileConnection()

    id_a = insert_fit_file(
        conn,
        garmin_activity_id=111,
        storage_path="/data/fit/2026/08/activity-111.fit",
        sha256=_SHA_A,
        size_bytes=100,
    )
    id_b = insert_fit_file(
        conn,
        garmin_activity_id=222,
        storage_path="/data/fit/2026/08/activity-222.fit",
        sha256=_SHA_B,
        size_bytes=200,
    )

    assert id_a != id_b
    assert len(conn.rows) == 2


def test_insert_fit_file_same_sha256_different_activity_raises_conflict() -> None:
    """BLOCKING 2 (review) — written first, before fix, as requested.

    Real scenario for this athlete (triathlon): a brick/multisport session can produce
    two distinct ``garmin_activity_id`` (e.g., parent and child running activity)
    whose 'original' download serves EXACTLY the same file — same content, same sha256.
    Before the fix, conflict re-read only compared ``sha256`` and returned the old
    ``fit_file_id`` without verifying which activity it really belonged to: activity
    222 would silently get a row actually pointing to 111 — permanent orphan for 222,
    false "archived" status, no resync possible without manual intervention. This test
    proves this case now raises :class:`FitFileShaConflictError` instead of lying.
    """
    conn = _FakeFitFileConnection()
    insert_fit_file(
        conn,
        garmin_activity_id=111,
        storage_path="/data/fit/2026/08/activity-111.fit",
        sha256=_SHA_A,
        size_bytes=100,
    )

    with pytest.raises(FitFileShaConflictError, match="111") as excinfo:
        insert_fit_file(
            conn,
            garmin_activity_id=222,
            storage_path="/data/fit/2026/08/activity-222.fit",
            sha256=_SHA_A,
            size_bytes=100,
        )

    # Losing activity must not appear anywhere in DB: no row created, no existing row
    # reassigned — orphaning must stay visible, not hidden.
    assert len(conn.rows) == 1
    assert conn.rows[0].garmin_activity_id == 111
    assert "222" in str(excinfo.value)


def test_insert_fit_file_raises_if_conflict_reread_finds_nothing() -> None:
    # Deliberately forced inconsistency (never observable with real PostgreSQL,
    # UNIQUE constraint guarantees row exists): covers insert_fit_file's defensive
    # guard rather than leaving it untested.
    conn = _FakeFitFileConnection()
    conn.rows.append(
        _Row(
            fit_file_id=1,
            garmin_activity_id=1,
            storage_path="x",
            sha256=_SHA_A,
            size_bytes=1,
        )
    )

    class _VanishingCursor(_FakeCursor):
        def execute(self, query: str, params: Any = None) -> None:
            normalized = " ".join(query.split())
            if normalized.startswith("SELECT fit_file_id, garmin_activity_id"):
                self._result = None
                return
            super().execute(query, params)

    # Replace bound method `cursor` on ONE instance to force re-read to fail without
    # duplicating entire `_FakeFitFileConnection` double. mypy normally flags this as
    # dangerous (bound method is not normally simple reassignable attribute); here it's
    # safe and local to this one test: the `type: ignore` is kept but justified rather
    # than left bare.
    conn.cursor = lambda: _VanishingCursor(conn)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="inconsistency"):
        insert_fit_file(
            conn,
            garmin_activity_id=2,
            storage_path="y",
            sha256=_SHA_A,
            size_bytes=1,
        )
