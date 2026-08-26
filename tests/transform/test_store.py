"""Tests for :mod:`t0.transform.store`, against an in-memory DB connection.

``FakeConnection``/``FakeCursor`` (defined below, local to this module -- not shared
with ``tests/garmin/fakes.py`` which serves a different purpose) implement
only the structural interface ``t0.transform.store.DBConnection``. They don't just
record queries: they also replay ``DELETE``/``INSERT``/``executemany`` on an in-memory
Python state, table by table, so that idempotence (two passes leave the same state)
is verifiable on the state itself, not just on the query sequence.

100% synthetic fixtures (no bytes from the athlete's real files), in the style of
``tests/transform/conftest.py``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from t0.fit.model import LapRow, SampleRow, SwimLengthRow
from t0.transform.rows import (
    ActivityHrHistogramRow,
    ActivityMetricRow,
    ActivityRow,
    CoreActivityBundle,
)
from t0.transform.store import store_activity_bundle

START = datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)
ACTIVITY_ID = 42
ATHLETE_ID = 7

_TABLE_RE = re.compile(r"(?:INSERT INTO|DELETE FROM) (core\.\w+)", re.IGNORECASE)


class FakeCursor:
    """In-memory cursor: records each query and replays DELETE/INSERT/executemany
    on ``conn.tables`` (a dict table -> list of parameter tuples)."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self._conn.executed.append((normalized, params))
        table = self._table_of(normalized)
        upper = normalized.upper()
        if upper.startswith("DELETE FROM"):
            activity_id = params[0]
            self._conn.tables.setdefault(table, [])
            self._conn.tables[table] = [
                row for row in self._conn.tables[table] if row[0] != activity_id
            ]
        elif upper.startswith("INSERT INTO CORE.ACTIVITY "):
            self._conn.activity[params[0]] = params

    def executemany(self, query: str, params_seq: Any) -> None:
        normalized = " ".join(query.split())
        rows = list(params_seq)
        self._conn.executed.append((normalized, rows))
        table = self._table_of(normalized)
        self._conn.tables.setdefault(table, [])
        self._conn.tables[table].extend(rows)

    @staticmethod
    def _table_of(normalized_query: str) -> str:
        match = _TABLE_RE.search(normalized_query)
        assert match is not None, f"query without recognizable core.* table: {normalized_query}"
        return match.group(1).lower()

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeConnection:
    """In-memory DB connection satisfying ``t0.transform.store.DBConnection``."""

    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    activity: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    tables: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


def _activity_row(**overrides: Any) -> ActivityRow:
    base: dict[str, Any] = {
        "activity_id": ACTIVITY_ID,
        "athlete_id": ATHLETE_ID,
        "sport_code": "running",
        "start_time": START,
        "start_time_local": START,
        "local_date": date(2026, 6, 1),
        "tz_offset_s": 7200,
        "duration_s": 3600,
    }
    base.update(overrides)
    return ActivityRow(**base)


def _sample(
    elapsed_s: int, *, power: int | None = None, heart_rate: int | None = None
) -> SampleRow:
    return SampleRow(
        activity_id=ACTIVITY_ID,
        t=START,
        elapsed_s=elapsed_s,
        power=power,
        heart_rate=heart_rate,
    )


def _bundle(**overrides: Any) -> CoreActivityBundle:
    base: dict[str, Any] = {
        "activity": _activity_row(),
        "samples": (),
        "laps": (),
        "swim_lengths": (),
        "hr_histogram": (),
        "power_curve": (),
        "metrics": (),
    }
    base.update(overrides)
    return CoreActivityBundle(**base)


def test_activity_written_before_its_samples() -> None:
    conn = FakeConnection()
    bundle = _bundle(samples=(_sample(0, power=200, heart_rate=140),))

    store_activity_bundle(conn, bundle)

    tables_touched = [FakeCursor._table_of(q) for q, _ in conn.executed]
    assert tables_touched[0] == "core.activity"
    assert "core.sample" in tables_touched[1:]


def test_replaying_same_bundle_leaves_same_state() -> None:
    conn = FakeConnection()
    bundle = _bundle(
        samples=(_sample(0, power=200, heart_rate=140), _sample(1, power=205, heart_rate=141)),
        hr_histogram=(ActivityHrHistogramRow(activity_id=ACTIVITY_ID, hr_bpm=140, seconds=2),),
    )

    store_activity_bundle(conn, bundle)
    state_after_first = {k: list(v) for k, v in conn.tables.items()}
    store_activity_bundle(conn, bundle)

    assert conn.tables == state_after_first
    assert conn.commit_count == 2
    delete_queries = [
        q for q, _ in conn.executed if q.upper().startswith("DELETE FROM CORE.SAMPLE")
    ]
    assert len(delete_queries) == 2  # one purge per pass, never a duplicate
    assert len(conn.tables["core.sample"]) == 2


def test_none_stays_none_never_becomes_zero() -> None:
    conn = FakeConnection()
    # Home-trainer session with power but no HR (real case: 4/19 sessions for this
    # athlete): trimp/hrtss must remain NULL, never 0.
    bundle = _bundle(
        samples=(_sample(0, power=200, heart_rate=None),),
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="trimp",
                method_code="hr",
                value=None,
                is_canonical=False,
                threshold_id=None,
            ),
        ),
    )

    store_activity_bundle(conn, bundle)

    sample_row = conn.tables["core.sample"][0]
    assert sample_row[8] is None  # heart_rate: neither 0 nor empty string
    metric_row = conn.tables["core.activity_metric"][0]
    assert metric_row[3] is None  # value


def test_samples_sent_in_single_batch() -> None:
    conn = FakeConnection()
    bundle = _bundle(samples=tuple(_sample(i, power=200) for i in range(500)))

    store_activity_bundle(conn, bundle)

    executemany_sample_calls = [
        params
        for q, params in conn.executed
        if q.upper().startswith("INSERT INTO CORE.SAMPLE") and isinstance(params, list)
    ]
    assert len(executemany_sample_calls) == 1
    assert len(executemany_sample_calls[0]) == 500


def test_swimming_without_samples_but_with_lengths() -> None:
    conn = FakeConnection()
    bundle = _bundle(
        activity=_activity_row(sport_code="lap_swimming", duration_s=1800),
        samples=(),
        swim_lengths=(
            SwimLengthRow(
                activity_id=ACTIVITY_ID,
                length_index=0,
                start_time=START,
                duration_s=30.5,
                length_type="active",
            ),
        ),
    )

    result = store_activity_bundle(conn, bundle)

    assert result.samples == 0
    assert result.swim_lengths == 1
    assert conn.tables.get("core.sample", []) == []
    assert len(conn.tables["core.swim_length"]) == 1
    # The DELETE from core.sample must still have occurred (unconditional full
    # replacement), even if no rows existed to delete.
    assert any(q.upper().startswith("DELETE FROM CORE.SAMPLE") for q, _ in conn.executed)


def test_canonical_method_change_between_two_passes() -> None:
    """First pass: hrTSS canonical (no power). Second pass: a power sensor appeared,
    power TSS becomes canonical. The partial unique index ``activity_metric_canonical_uq``
    would prevent both canonical rows from coexisting with different ``method_code``
    values -- the prior DELETE in this module must make this case risk-free, never a
    silent violation."""
    conn = FakeConnection()
    first_pass = _bundle(
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="tss",
                method_code="hr",
                value=80.0,
                is_canonical=True,
                threshold_id=4,
            ),
        )
    )
    store_activity_bundle(conn, first_pass)
    assert len(conn.tables["core.activity_metric"]) == 1
    assert conn.tables["core.activity_metric"][0][2] == "hr"
    assert conn.tables["core.activity_metric"][0][4] is True

    second_pass = _bundle(
        metrics=(
            ActivityMetricRow(
                activity_id=ACTIVITY_ID,
                metric="tss",
                method_code="power",
                value=95.0,
                is_canonical=True,
                threshold_id=1,
            ),
        )
    )
    store_activity_bundle(conn, second_pass)

    rows = conn.tables["core.activity_metric"]
    assert len(rows) == 1  # the old hr row was properly purged, not just ignored
    assert rows[0][2] == "power"
    assert rows[0][4] is True


def test_laps_deleted_then_reinserted_on_second_pass() -> None:
    conn = FakeConnection()
    bundle_v1 = _bundle(
        laps=(
            LapRow(activity_id=ACTIVITY_ID, lap_index=0, start_time=START, duration_s=600),
            LapRow(activity_id=ACTIVITY_ID, lap_index=1, start_time=START, duration_s=600),
        )
    )
    store_activity_bundle(conn, bundle_v1)
    assert len(conn.tables["core.activity_lap"]) == 2

    # Corrected decoder: the two laps are merged into one.
    bundle_v2 = _bundle(
        laps=(LapRow(activity_id=ACTIVITY_ID, lap_index=0, start_time=START, duration_s=1200),),
    )
    store_activity_bundle(conn, bundle_v2)

    rows = conn.tables["core.activity_lap"]
    assert len(rows) == 1  # no phantom rows from the old split
    assert rows[0][3] == 1200


def test_ingested_at_never_rewritten_and_single_commit() -> None:
    conn = FakeConnection()
    bundle = _bundle()

    result = store_activity_bundle(conn, bundle)

    assert conn.commit_count == 1
    assert result.activity_id == ACTIVITY_ID
    query, params = conn.executed[0]
    assert "INSERT INTO core.activity" in query
    assert "ON CONFLICT (activity_id) DO UPDATE" in query
    assert "ingested_at" not in query.split("ON CONFLICT")[1]  # never rewritten
    assert "updated_at = now()" in query
    assert params[0] == ACTIVITY_ID
