"""Tests for :mod:`t0.transform.wellness_runner`.

Two levels, deliberately separated.

``merge_day`` is PURE, so it is tested directly on hand-written payloads: it is where the
wellness endpoints collapse into a single ``core.daily_wellness`` row, and where an endpoint
answering empty must leave a hole rather than blank a column another endpoint filled.

``transform_wellness_days`` is tested against ``FakeConnection``, a scripted in-memory
connection that answers each of the pass's four queries by pattern. What matters there is
not the SQL but the three decisions the pass makes: which days to reprocess (content
digest, not watermark), that a failing day is rolled back and skipped rather than aborting
the batch, and that ``force`` reprocesses everything.

All payloads are synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from t0.transform.wellness_runner import (
    DayPayloads,
    WellnessReport,
    format_wellness_report,
    merge_day,
    transform_wellness_days,
)

ATHLETE_ID = 7
DAY = date(2026, 6, 1)
MIDNIGHT_MS = 1780272000000


def _day(payloads: dict[str, Any], hashes: dict[str, bytes | None] | None = None) -> DayPayloads:
    return DayPayloads(
        local_date=DAY,
        payloads=payloads,
        hashes=hashes if hashes is not None else dict.fromkeys(payloads, b"\x01" * 32),
    )


# -- merge_day ---------------------------------------------------------------------------


def test_endpoints_contribute_to_a_single_daily_row() -> None:
    merged = merge_day(
        _day(
            {
                "get_rhr_day": {
                    "allMetrics": {
                        "metricsMap": {
                            "WELLNESS_RESTING_HEART_RATE": [
                                {"calendarDate": "2026-06-01", "value": 48}
                            ]
                        }
                    }
                },
                "get_hrv_data": {"hrvSummary": {"lastNightAvg": 62.0, "status": "BALANCED"}},
                "get_stress_data": {"avgStressLevel": 31},
                "get_steps_data": [{"steps": 12345}],
                "get_training_status": {
                    "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 58.4}}
                },
            }
        ),
        athlete_id=ATHLETE_ID,
    )

    daily = merged.daily
    assert (daily.athlete_id, daily.local_date) == (ATHLETE_ID, DAY)
    assert (daily.resting_hr, daily.hrv_last_night_avg, daily.hrv_status) == (48, 62.0, "BALANCED")
    assert (daily.avg_stress, daily.steps, daily.vo2max_running) == (31, 12345, 58.4)


def test_absent_endpoints_leave_holes_never_zeroes() -> None:
    merged = merge_day(_day({}), athlete_id=ATHLETE_ID)

    daily = merged.daily
    assert daily.resting_hr is None
    assert daily.avg_stress is None
    assert daily.steps is None
    assert daily.body_battery_high is None
    assert daily.weight_kg is None
    assert daily.training_readiness is None
    assert merged.sleep is None
    assert (merged.stages, merged.samples) == ((), ())


def test_max_metrics_is_unwrapped_from_its_single_element_list() -> None:
    # get_max_metrics answers with a LIST wrapping the object get_training_status returns bare.
    merged = merge_day(
        _day({"get_max_metrics": [{"generic": {"vo2MaxPreciseValue": 57.1}}]}),
        athlete_id=ATHLETE_ID,
    )

    assert merged.daily.vo2max_running == 57.1


def test_training_status_wins_over_max_metrics_but_an_empty_one_does_not_blank_the_value() -> None:
    merged = merge_day(
        _day(
            {
                "get_training_status": {"mostRecentVO2Max": None},
                "get_max_metrics": [{"generic": {"vo2MaxPreciseValue": 57.1}}],
            }
        ),
        athlete_id=ATHLETE_ID,
    )

    # The preferred endpoint yielded nothing, so the fallback still gets its turn.
    assert merged.daily.vo2max_running == 57.1


def test_samples_of_all_three_owned_metrics_are_collected() -> None:
    merged = merge_day(
        _day(
            {
                "get_hrv_data": {
                    "hrvReadings": [{"readingTimeGMT": "2026-06-01T01:05:00.0", "hrvValue": 66}]
                },
                "get_stress_data": {
                    "stressValueDescriptorsDTOList": [
                        {"key": "timestamp", "index": 0},
                        {"key": "stressLevel", "index": 1},
                    ],
                    "stressValuesArray": [[MIDNIGHT_MS, 25], [MIDNIGHT_MS + 180000, -1]],
                },
                "get_body_battery": [
                    {
                        "bodyBatteryValueDescriptorDTOList": [
                            {
                                "bodyBatteryValueDescriptorKey": "timestamp",
                                "bodyBatteryValueDescriptorIndex": 0,
                            },
                            {
                                "bodyBatteryValueDescriptorKey": "bodyBatteryLevel",
                                "bodyBatteryValueDescriptorIndex": 1,
                            },
                        ],
                        "bodyBatteryValuesArray": [[MIDNIGHT_MS, 88]],
                    }
                ],
            }
        ),
        athlete_id=ATHLETE_ID,
    )

    # HRV + one stress point (the -1 sentinel dropped) + one Body Battery point.
    assert len(merged.samples) == 3
    assert merged.daily.body_battery_high == 88


def test_the_day_digest_folds_every_payload_hash() -> None:
    hashes: dict[str, bytes | None] = {"get_sleep_data": b"\x01" * 32, "get_hrv_data": b"\x02" * 32}
    same_content_other_order: dict[str, bytes | None] = {
        "get_hrv_data": b"\x02" * 32,
        "get_sleep_data": b"\x01" * 32,
    }

    first = merge_day(_day({}, hashes), athlete_id=ATHLETE_ID)
    second = merge_day(_day({}, same_content_other_order), athlete_id=ATHLETE_ID)
    changed = merge_day(
        _day({}, {"get_sleep_data": b"\x09" * 32, "get_hrv_data": b"\x02" * 32}),
        athlete_id=ATHLETE_ID,
    )

    assert first.source_sha256 == second.source_sha256
    assert first.source_sha256 != changed.source_sha256


# -- transform_wellness_days --------------------------------------------------------------

DIGEST_A = b"\xaa" * 32
DIGEST_B = b"\xbb" * 32


@dataclass
class FakeConnection:
    """Scripted connection: answers the pass's queries, records writes, counts rollbacks."""

    #: scope_date -> {endpoint: payload_sha256}
    raw_days: dict[date, dict[str, bytes | None]] = field(default_factory=dict)
    #: local_date -> source_sha256 already in core.daily_wellness
    stored: dict[date, bytes | None] = field(default_factory=dict)
    #: days whose write must raise, to exercise the failure branch
    failing: set[date] = field(default_factory=set)
    written: list[date] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._rows: list[Any] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return self._rows

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split()).upper()
        conn = self._conn
        if normalized.startswith("SELECT DISTINCT SCOPE_DATE"):
            since = params[1]
            self._rows = [(day,) for day in sorted(conn.raw_days) if since is None or day >= since][
                : params[3]
            ]
        elif normalized.startswith("SELECT DISTINCT ON (SCOPE_DATE, ENDPOINT)"):
            wanted = set(params[1])
            self._rows = [
                (day, endpoint, digest)
                for day in sorted(conn.raw_days)
                if day in wanted
                for endpoint, digest in sorted(conn.raw_days[day].items())
            ]
        elif normalized.startswith("SELECT LOCAL_DATE, SOURCE_SHA256"):
            wanted = set(params[1])
            self._rows = [(day, d) for day, d in conn.stored.items() if day in wanted]
        elif normalized.startswith("SELECT DISTINCT ON (ENDPOINT)"):
            day = params[1]
            self._rows = [
                (endpoint, {}, digest)
                for endpoint, digest in sorted(conn.raw_days.get(day, {}).items())
            ]
        elif normalized.startswith("INSERT INTO CORE.DAILY_WELLNESS"):
            day = params[1]
            if day in conn.failing:
                msg = "synthetic write failure"
                raise RuntimeError(msg)
            conn.written.append(day)
            conn.stored[day] = params[-1]

    def executemany(self, query: str, params_seq: Any) -> None:
        list(params_seq)


def _conn_with_two_days() -> FakeConnection:
    return FakeConnection(
        raw_days={
            date(2026, 5, 31): {"get_sleep_data": DIGEST_A},
            date(2026, 6, 1): {"get_sleep_data": DIGEST_B},
        }
    )


def test_a_first_pass_stores_every_day() -> None:
    conn = _conn_with_two_days()

    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID)

    assert (report.stored, report.unchanged, report.failed) == (2, 0, 0)
    assert conn.written == [date(2026, 5, 31), date(2026, 6, 1)], "oldest first"


def test_a_second_pass_on_identical_content_rewrites_nothing() -> None:
    conn = _conn_with_two_days()
    transform_wellness_days(conn, athlete_id=ATHLETE_ID)
    conn.written.clear()

    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID)

    # The nightly lookback re-polls the same days: identical content must cost nothing.
    assert (report.stored, report.unchanged) == (0, 2)
    assert conn.written == []


def test_a_republished_day_is_reprocessed_even_though_its_date_is_old() -> None:
    conn = _conn_with_two_days()
    transform_wellness_days(conn, athlete_id=ATHLETE_ID)
    conn.written.clear()
    # Garmin recomputes the older night days later: a watermark would never revisit it.
    conn.raw_days[date(2026, 5, 31)]["get_sleep_data"] = b"\xcc" * 32

    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID)

    assert (report.stored, report.unchanged) == (1, 1)
    assert conn.written == [date(2026, 5, 31)]


def test_force_reprocesses_days_no_digest_could_flag() -> None:
    conn = _conn_with_two_days()
    transform_wellness_days(conn, athlete_id=ATHLETE_ID)
    conn.written.clear()

    # The escape hatch for the one change a content hash cannot see: this code itself.
    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID, force=True)

    assert report.stored == 2
    assert len(conn.written) == 2


def test_since_and_limit_bound_the_pass() -> None:
    conn = _conn_with_two_days()

    assert transform_wellness_days(conn, athlete_id=ATHLETE_ID, since=date(2026, 6, 1)).stored == 1

    conn2 = _conn_with_two_days()
    assert transform_wellness_days(conn2, athlete_id=ATHLETE_ID, limit=1).stored == 1


def test_one_failing_day_is_rolled_back_and_the_others_still_land() -> None:
    conn = _conn_with_two_days()
    conn.failing = {date(2026, 5, 31)}

    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID)

    assert (report.stored, report.failed) == (1, 1)
    assert conn.rollbacks == 1
    assert conn.written == [date(2026, 6, 1)]
    failure = next(o for o in report.outcomes if o.status == "failed")
    assert "synthetic write failure" in failure.detail


def test_a_failed_day_is_retried_on_the_next_pass() -> None:
    conn = _conn_with_two_days()
    conn.failing = {date(2026, 5, 31)}
    transform_wellness_days(conn, athlete_id=ATHLETE_ID)
    conn.failing.clear()
    conn.written.clear()

    report = transform_wellness_days(conn, athlete_id=ATHLETE_ID)

    # Nothing was stored for that day, so its digest still differs: it comes back on its own.
    assert report.stored == 1
    assert conn.written == [date(2026, 5, 31)]


def test_an_empty_database_reports_nothing_rather_than_failing() -> None:
    report = transform_wellness_days(FakeConnection(), athlete_id=ATHLETE_ID)

    assert report.outcomes == ()
    assert (report.stored, report.unchanged, report.failed) == (0, 0, 0)


def test_the_report_names_the_failed_days() -> None:
    conn = _conn_with_two_days()
    conn.failing = {date(2026, 5, 31)}

    text = format_wellness_report(transform_wellness_days(conn, athlete_id=ATHLETE_ID))

    assert "1 day(s) stored" in text
    assert "2026-05-31" in text


def test_a_clean_report_lists_no_day() -> None:
    text = format_wellness_report(WellnessReport(outcomes=()))

    assert text.splitlines() == [
        "Wellness: 0 day(s) stored, 0 unchanged, 0 failed; 0 night(s), 0 intraday sample(s)."
    ]
