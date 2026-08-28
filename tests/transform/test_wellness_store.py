"""Tests for :mod:`t0.transform.wellness_store`, against an in-memory connection.

``FakeConnection``/``FakeCursor`` implement only the structural interface
``t0.transform.wellness_store.DBConnection``, and REPLAY the statements on Python
dicts and lists table by table. That is the point: the property under test is idempotence
-- running the same day twice must leave the same state, and running a SHRUNK day must not
leave the removed rows behind -- and that can only be observed on the state, never on the
query text.

All rows below are synthetic, in the style of ``tests/transform/conftest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from t0.transform.wellness import (
    METRIC_ID_BODY_BATTERY,
    METRIC_ID_HRV,
    METRIC_ID_STRESS,
    DailyWellnessRow,
    SleepSessionRow,
    SleepStageRow,
    WellnessSampleRow,
)
from t0.transform.wellness_store import combine_source_hashes, store_wellness_day

ATHLETE_ID = 7
LOCAL_DATE = date(2026, 6, 1)
NIGHT_START = datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
NIGHT_END = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)


@dataclass
class FakeConnection:
    """In-memory stand-in replaying the store's statements."""

    daily: dict[tuple[int, date], tuple[Any, ...]] = field(default_factory=dict)
    sessions: dict[tuple[int, date], tuple[Any, ...]] = field(default_factory=dict)
    stages: list[tuple[Any, ...]] = field(default_factory=list)
    samples: list[tuple[Any, ...]] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    commits: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split()).upper()
        self._conn.executed.append(normalized)
        if normalized.startswith("INSERT INTO CORE.DAILY_WELLNESS"):
            self._conn.daily[(params[0], params[1])] = params
        elif normalized.startswith("INSERT INTO CORE.SLEEP_SESSION"):
            self._conn.sessions[(params[0], params[1])] = params
        elif normalized.startswith("DELETE FROM CORE.SLEEP_STAGE"):
            athlete_id, wake_date = params
            self._conn.stages = [
                row for row in self._conn.stages if (row[0], row[1]) != (athlete_id, wake_date)
            ]
        elif normalized.startswith("DELETE FROM CORE.WELLNESS_SAMPLE"):
            athlete_id, local_date, metric_ids = params
            self._conn.samples = [
                row
                for row in self._conn.samples
                if not (row[2] == athlete_id and row[4] == local_date and row[0] in metric_ids)
            ]

    def executemany(self, query: str, params_seq: Any) -> None:
        normalized = " ".join(query.split()).upper()
        self._conn.executed.append(normalized)
        rows = list(params_seq)
        if normalized.startswith("INSERT INTO CORE.SLEEP_STAGE"):
            self._conn.stages.extend(rows)
        elif normalized.startswith("INSERT INTO CORE.WELLNESS_SAMPLE"):
            # Replays the DDL's ON CONFLICT (metric_id, t) DO NOTHING: the PK pair
            # already present survives, the new row is dropped.
            existing = {(row[0], row[1]) for row in self._conn.samples}
            self._conn.samples.extend(row for row in rows if (row[0], row[1]) not in existing)


def _daily(**overrides: Any) -> DailyWellnessRow:
    return DailyWellnessRow(
        athlete_id=ATHLETE_ID, local_date=LOCAL_DATE, resting_hr=48, **overrides
    )


def _session() -> SleepSessionRow:
    return SleepSessionRow(
        athlete_id=ATHLETE_ID,
        wake_date=LOCAL_DATE,
        sleep_start=NIGHT_START,
        sleep_end=NIGHT_END,
        sleep_start_local=NIGHT_START.replace(tzinfo=None),
        duration_s=28800,
        deep_s=3600,
        light_s=18000,
        rem_s=5400,
        awake_s=1800,
        sleep_score=82,
        avg_spo2=95.0,
        avg_respiration=13.5,
        source_sha256=None,
    )


def _stage(minute: int, stage: str) -> SleepStageRow:
    start = datetime(2026, 5, 31, 22, minute, tzinfo=UTC)
    return SleepStageRow(
        athlete_id=ATHLETE_ID,
        wake_date=LOCAL_DATE,
        stage_start=start,
        stage_end=start.replace(minute=minute + 10),
        stage=stage,
    )


def _sample(metric_id: int, minute: int, value: float) -> WellnessSampleRow:
    return WellnessSampleRow(
        metric_id=metric_id,
        t=datetime(2026, 6, 1, 8, minute, tzinfo=UTC),
        athlete_id=ATHLETE_ID,
        value=value,
        local_date=LOCAL_DATE,
    )


def _store(conn: FakeConnection, *, stages: Any, samples: Any) -> Any:
    return store_wellness_day(
        conn,
        daily=_daily(),
        source_sha256=b"\x01" * 32,
        sleep=_session(),
        stages=stages,
        samples=samples,
        sample_metric_ids=(METRIC_ID_STRESS, METRIC_ID_BODY_BATTERY, METRIC_ID_HRV),
    )


def test_a_day_is_written_once_and_committed_once() -> None:
    conn = FakeConnection()

    result = _store(
        conn,
        stages=[_stage(0, "deep"), _stage(20, "light")],
        samples=[_sample(METRIC_ID_HRV, 5, 62)],
    )

    assert (result.daily_wellness, result.sleep_sessions, result.sleep_stages, result.samples) == (
        1,
        1,
        2,
        1,
    )
    assert conn.commits == 1, "one transaction per day: a half-written day is invisible"
    assert len(conn.daily) == 1
    assert len(conn.sessions) == 1


def test_running_the_same_day_twice_leaves_the_same_state() -> None:
    conn = FakeConnection()
    stages = [_stage(0, "deep"), _stage(20, "light")]
    samples = [_sample(METRIC_ID_HRV, 5, 62), _sample(METRIC_ID_STRESS, 6, 25)]

    _store(conn, stages=stages, samples=samples)
    first = (dict(conn.daily), dict(conn.sessions), list(conn.stages), list(conn.samples))
    _store(conn, stages=stages, samples=samples)

    assert (dict(conn.daily), dict(conn.sessions), list(conn.stages), list(conn.samples)) == first


def test_a_republished_night_loses_the_segments_that_disappeared() -> None:
    conn = FakeConnection()
    _store(conn, stages=[_stage(0, "deep"), _stage(20, "light"), _stage(40, "rem")], samples=[])
    assert len(conn.stages) == 3

    # Garmin recomputes the night into fewer, longer segments.
    _store(conn, stages=[_stage(0, "deep")], samples=[])

    # ON CONFLICT would have kept the two ghosts, invisible in a diff and real in a panel.
    assert len(conn.stages) == 1


def test_a_night_republished_without_levels_loses_its_phases() -> None:
    conn = FakeConnection()
    _store(conn, stages=[_stage(0, "deep")], samples=[])

    _store(conn, stages=[], samples=[])

    assert conn.stages == []
    assert len(conn.sessions) == 1, "the summary survives; only the phases go"


def test_an_empty_series_still_clears_the_previous_points() -> None:
    conn = FakeConnection()
    _store(conn, stages=[], samples=[_sample(METRIC_ID_STRESS, 5, 25)])
    assert len(conn.samples) == 1

    # The scope comes from sample_metric_ids, not from the rows to insert: deriving it from
    # the rows would make exactly this case a no-op and keep yesterday's points forever.
    _store(conn, stages=[], samples=[])

    assert conn.samples == []


def test_samples_of_other_days_are_untouched() -> None:
    conn = FakeConnection()
    other = (
        METRIC_ID_STRESS,
        datetime(2026, 5, 30, 8, 0, tzinfo=UTC),
        ATHLETE_ID,
        30.0,
        date(2026, 5, 30),
    )
    conn.samples.append(other)

    _store(conn, stages=[], samples=[_sample(METRIC_ID_STRESS, 5, 25)])

    assert other in conn.samples


def test_a_reading_already_stored_under_another_day_is_not_duplicated() -> None:
    # A night-boundary reading (observed: HRV at 23:15) arrives in the payloads of
    # TWO consecutive days. The DELETE scopes THIS day only, so the row stored under
    # the previous day's local_date survives it -- without ON CONFLICT DO NOTHING the
    # insert collides on wellness_sample_pk and the whole day fails.
    conn = FakeConnection()
    late_evening = datetime(2026, 6, 1, 23, 15, tzinfo=UTC)
    # First write happened while processing the PREVIOUS day: its local_date is
    # the previous day, which the DELETE of the current day does not touch.
    first_day_row = (
        METRIC_ID_HRV,
        late_evening,
        ATHLETE_ID,
        40.0,
        LOCAL_DATE - timedelta(days=1),
    )
    conn.samples.append(first_day_row)

    next_day = LOCAL_DATE
    duplicate = WellnessSampleRow(
        metric_id=METRIC_ID_HRV,
        t=late_evening,
        athlete_id=ATHLETE_ID,
        value=40.0,
        local_date=next_day,
    )
    _store(conn, stages=[], samples=[duplicate])

    # First write wins: the same PK is NOT stored twice, and the surviving row keeps
    # the day tag it was first written with.
    assert conn.samples.count(first_day_row) == 1


def test_phases_are_dropped_when_no_session_could_be_extracted() -> None:
    conn = FakeConnection()

    result = store_wellness_day(
        conn,
        daily=_daily(),
        sleep=None,
        stages=[_stage(0, "deep")],
        samples=[],
        sample_metric_ids=(METRIC_ID_HRV,),
    )

    # core.sleep_stage has an FK to core.sleep_session: the database would refuse them.
    assert conn.stages == []
    assert (result.sleep_sessions, result.sleep_stages) == (0, 0)
    assert len(conn.daily) == 1, "the daily row is written regardless"


def test_the_session_is_inserted_before_its_phases() -> None:
    conn = FakeConnection()

    _store(conn, stages=[_stage(0, "deep")], samples=[])

    session_at = next(
        i for i, q in enumerate(conn.executed) if "INSERT INTO CORE.SLEEP_SESSION" in q
    )
    stage_at = next(i for i, q in enumerate(conn.executed) if "INSERT INTO CORE.SLEEP_STAGE" in q)
    assert session_at < stage_at


class TestCombineSourceHashes:
    def test_is_order_independent(self) -> None:
        a, b, c = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32

        assert combine_source_hashes([a, b, c]) == combine_source_hashes([c, a, b])

    def test_changes_when_any_payload_changes(self) -> None:
        a, b = b"\x01" * 32, b"\x02" * 32

        assert combine_source_hashes([a, b]) != combine_source_hashes([a, b[:-1] + b"\x03"])

    def test_ignores_missing_digests_and_returns_none_when_all_absent(self) -> None:
        a = b"\x01" * 32

        assert combine_source_hashes([a, None]) == combine_source_hashes([a])
        assert combine_source_hashes([None, None]) is None
        assert combine_source_hashes([]) is None
