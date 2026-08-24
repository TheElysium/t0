"""PostgreSQL connection stub shared by tests/api/.

Models ``raw.fit_file`` as close as possible to real schema (``migrations/002_raw.sql``
+ ``012_raw_fit_file_manual_upload.sql``), not as close as convenient to write: the
``UNIQUE (sha256)`` constraint (``fit_file_sha_uq``) is applied via the same query pair
that :func:`garminator.garmin.raw_store.insert_fit_file` actually emits
(``INSERT ... ON CONFLICT (sha256) DO NOTHING RETURNING fit_file_id``, then on conflict,
a read-back by ``sha256``). A stub accepting a second ``INSERT`` for an already-present
``sha256`` would let a test pass that PostgreSQL would really refuse — precisely the
defect not to reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FitFileRow:
    """Minimal reflection of a ``raw.fit_file`` row, for only the columns that
    :func:`garminator.garmin.raw_store.insert_fit_file` reads or writes."""

    fit_file_id: int
    garmin_activity_id: int | None
    storage_path: str
    sha256: bytes
    size_bytes: int


class FakeCursor:
    def __init__(self, conn: FakeRawStoreConnection) -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split()).upper()
        self._conn.executed.append(normalized)
        if normalized.startswith("INSERT INTO RAW.FIT_FILE"):
            garmin_activity_id, storage_path, sha256, size_bytes = params
            if sha256 in self._conn.rows_by_sha:
                # ON CONFLICT (sha256) DO NOTHING: no row created, RETURNING produces
                # nothing — exactly what fetchone() must reflect here.
                self._result = []
                return
            self._conn.next_id += 1
            row = FitFileRow(
                fit_file_id=self._conn.next_id,
                garmin_activity_id=garmin_activity_id,
                storage_path=storage_path,
                sha256=sha256,
                size_bytes=size_bytes,
            )
            self._conn.rows_by_sha[sha256] = row
            self._result = [(row.fit_file_id,)]
        elif normalized.startswith("SELECT FIT_FILE_ID, GARMIN_ACTIVITY_ID FROM RAW.FIT_FILE"):
            (sha256,) = params
            existing = self._conn.rows_by_sha.get(sha256)
            self._result = (
                [] if existing is None else [(existing.fit_file_id, existing.garmin_activity_id)]
            )
        else:
            raise AssertionError(f"SQL query not modeled by this stub: {normalized!r}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeRawStoreConnection:
    """Stub for :class:`garminator.garmin.raw_store.DBConnection`."""

    rows_by_sha: dict[bytes, FitFileRow] = field(default_factory=dict)
    next_id: int = 0
    executed: list[str] = field(default_factory=list)
    commits: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def preload(
        self, *, sha256: bytes, garmin_activity_id: int | None, storage_path: str = "/preexisting"
    ) -> FitFileRow:
        """Simulate a ``raw.fit_file`` row already present before the tested call — to
        reproduce a ``sha256`` conflict (see ``FitFileShaConflictError``)."""
        self.next_id += 1
        row = FitFileRow(
            fit_file_id=self.next_id,
            garmin_activity_id=garmin_activity_id,
            storage_path=storage_path,
            sha256=sha256,
            size_bytes=0,
        )
        self.rows_by_sha[sha256] = row
        return row


@pytest.fixture
def raw_store_conn() -> FakeRawStoreConnection:
    return FakeRawStoreConnection()
