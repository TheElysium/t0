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


@dataclass
class CoreMetricRow:
    """Minimal reflection of a ``core.activity_metric`` row (migrations/005)."""

    activity_id: int
    metric: str
    method_code: str
    value: float | None
    is_canonical: bool


class FakeCoreCursor:
    def __init__(self, conn: FakeCoreConnection) -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split()).upper()
        self._conn.executed.append(normalized)
        if normalized.startswith("SELECT NEEDS_MANUAL_RPE FROM CORE.ACTIVITY"):
            (activity_id,) = params
            needs = self._conn.activities.get(activity_id)
            self._result = [] if needs is None else [(needs,)]
        elif normalized.startswith("SELECT METHOD_CODE FROM CORE.ACTIVITY_METRIC"):
            activity_id, metric = params
            canonical = [
                row
                for row in self._conn.metrics.values()
                if row.activity_id == activity_id and row.metric == metric and row.is_canonical
            ]
            # Real schema: partial unique index guarantees at most one; a stub returning
            # two would let an invariant violation pass unnoticed.
            assert len(canonical) <= 1, "two canonical rows: index invariant violated"
            self._result = [] if not canonical else [(canonical[0].method_code,)]
        elif normalized.startswith("DELETE FROM CORE.ACTIVITY_METRIC"):
            activity_id, metric, method_code = params
            self._conn.metrics.pop((activity_id, metric, method_code), None)
            self._result = []
        elif normalized.startswith("UPDATE CORE.ACTIVITY_METRIC SET IS_CANONICAL"):
            activity_id, metric = params
            for row in self._conn.metrics.values():
                if row.activity_id == activity_id and row.metric == metric:
                    row.is_canonical = False
            self._result = []
        elif normalized.startswith("INSERT INTO CORE.ACTIVITY_METRIC"):
            activity_id, metric, method_code, value, is_canonical, _threshold_id = params
            key = (activity_id, metric, method_code)
            if is_canonical:
                for row in self._conn.metrics.values():
                    if row.activity_id == activity_id and row.metric == metric and row.is_canonical:
                        raise AssertionError(
                            "INSERT violates activity_metric_canonical_uq "
                            f"({activity_id}, {metric}): {row.method_code} already canonical"
                        )
            self._conn.metrics[key] = CoreMetricRow(
                activity_id=activity_id,
                metric=metric,
                method_code=method_code,
                value=value,
                is_canonical=is_canonical,
            )
            self._result = []
        elif normalized.startswith("UPDATE CORE.ACTIVITY SET NEEDS_MANUAL_RPE"):
            (activity_id,) = params
            assert activity_id in self._conn.activities, "activity vanished mid-transaction"
            self._conn.activities[activity_id] = False
            self._result = []
        else:
            raise AssertionError(f"SQL query not modeled by this stub: {normalized!r}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def __enter__(self) -> FakeCoreCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeCoreConnection:
    """Stub for the ``core.*`` side of :class:`garminator.garmin.raw_store.DBConnection`,
    as exercised by ``garminator.api.rpe``.

    Models the two tables the RPE endpoint touches — ``core.activity``
    (``needs_manual_rpe`` flag only) and ``core.activity_metric`` (keyed by its real
    primary key ``(activity_id, metric, method_code)``) — and enforces the same
    single-canonical invariant as the partial unique index
    ``activity_metric_canonical_uq`` (migrations/005): a stub accepting a second
    canonical INSERT would let a test pass that PostgreSQL would really refuse.
    """

    activities: dict[int, bool] = field(default_factory=dict)
    metrics: dict[tuple[int, str, str], CoreMetricRow] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    commits: int = 0

    def cursor(self) -> FakeCoreCursor:
        return FakeCoreCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def add_activity(self, activity_id: int, *, needs_manual_rpe: bool = False) -> None:
        """Simulate a pre-existing ``core.activity`` row."""
        self.activities[activity_id] = needs_manual_rpe

    def set_canonical_tss(self, activity_id: int, method_code: str, value: float | None) -> None:
        """Simulate a pre-existing canonical TSS from another method."""
        self.metrics[(activity_id, "tss", method_code)] = CoreMetricRow(
            activity_id=activity_id,
            metric="tss",
            method_code=method_code,
            value=value,
            is_canonical=True,
        )


@pytest.fixture
def core_conn() -> FakeCoreConnection:
    return FakeCoreConnection()
