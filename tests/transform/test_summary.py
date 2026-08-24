"""Tests for :mod:`garminator.transform.summary` -- 100% synthetic fixtures, no real
PostgreSQL connection (:class:`_FakeThresholdDB` in-memory for ``load_thresholds``, cf.
layer mandate)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from garminator.garmin.raw_store import payload_sha256
from garminator.transform.summary import (
    DISCIPLINE_BY_SPORT_CODE,
    SPORT_CODE_BY_TYPE_KEY,
    SportInconnuError,
    build_activity_context,
    load_thresholds,
    select_thresholds,
)

ACTIVITY_ID = 123456789


def base_summary(**overrides: Any) -> dict[str, Any]:
    """Minimal but complete Garmin summary in the style of a ``get_activities_by_date``
    -- all values are invented and match no real session."""
    summary: dict[str, Any] = {
        "activityId": ACTIVITY_ID,
        "activityName": "Evening ride",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-06-01 19:00:00",
        "startTimeGMT": "2026-06-01 17:00:00",
        "duration": 3600.0,
        "elapsedDuration": 3650.0,
        "movingDuration": 3580.0,
        "distance": 10000.0,
        "elevationGain": 120.0,
        "elevationLoss": 115.0,
        "calories": 650,
        "parentId": None,
    }
    summary.update(overrides)
    return summary


class TestSportMapping:
    @pytest.mark.parametrize("sport_code", sorted(set(SPORT_CODE_BY_TYPE_KEY.values())))
    def test_all_12_sport_codes_are_reachable(self, sport_code: str) -> None:
        # Counter-test of expected coverage per spec: each sport_code existing in the
        # database must be reachable by at least one real Garmin typeKey.
        assert sport_code in DISCIPLINE_BY_SPORT_CODE

    def test_exactly_twelve_sport_codes_in_discipline_by_sport_code(self) -> None:
        assert len(DISCIPLINE_BY_SPORT_CODE) == 12

    def test_all_disciplines_are_valid(self) -> None:
        assert set(DISCIPLINE_BY_SPORT_CODE.values()) <= {"bike", "run", "swim", "other"}

    @pytest.mark.parametrize(
        ("type_key", "sport_code", "discipline"),
        [
            ("running", "running", "run"),
            ("trail_running", "trail_running", "run"),
            ("treadmill_running", "treadmill", "run"),
            ("indoor_running", "treadmill", "run"),
            ("track_running", "running", "run"),
            ("cycling", "cycling", "bike"),
            ("road_biking", "cycling", "bike"),
            ("indoor_cycling", "virtual_cycling", "bike"),
            ("virtual_ride", "virtual_cycling", "bike"),
            ("gravel_cycling", "cycling", "bike"),
            ("mountain_biking", "cycling", "bike"),
            ("lap_swimming", "lap_swimming", "swim"),
            ("open_water_swimming", "open_water", "swim"),
            ("hiking", "hiking", "other"),
            ("resort_skiing_snowboarding_ws", "resort_skiing", "other"),
            ("multi_sport", "multisport", "other"),
            ("transition", "transition", "other"),
            ("strength_training", "strength", "other"),
        ],
    )
    def test_type_key_to_sport_code_and_discipline_mapping(
        self, type_key: str, sport_code: str, discipline: str
    ) -> None:
        context = build_activity_context(
            base_summary(activityType={"typeKey": type_key}, parentId=None)
        )
        assert context.sport_code == sport_code
        assert context.discipline == discipline

    def test_unknown_type_key_raises_sport_error_with_type_key_and_activity_id(
        self,
    ) -> None:
        summary = base_summary(activityType={"typeKey": "paddleboarding_v2_wat"})
        with pytest.raises(SportInconnuError) as exc_info:
            build_activity_context(summary)
        assert "paddleboarding_v2_wat" in str(exc_info.value)
        assert str(ACTIVITY_ID) in str(exc_info.value)
        assert exc_info.value.type_key == "paddleboarding_v2_wat"
        assert exc_info.value.activity_id == ACTIVITY_ID

    def test_unknown_type_key_does_not_fall_back_to_running_or_other(self) -> None:
        # Explicit safeguard against silent fallback that the spec forbids.
        with pytest.raises(SportInconnuError):
            build_activity_context(base_summary(activityType={"typeKey": "quidditch"}))


class TestMultisport:
    def test_multi_sport_is_marked_is_parent(self) -> None:
        context = build_activity_context(
            base_summary(activityType={"typeKey": "multi_sport"}, parentId=None)
        )
        assert context.is_parent is True

    def test_non_multisport_activity_is_not_is_parent(self) -> None:
        context = build_activity_context(base_summary())
        assert context.is_parent is False

    def test_parent_id_present_becomes_parent_activity_id(self) -> None:
        context = build_activity_context(base_summary(parentId=999))
        assert context.parent_activity_id == 999

    def test_parent_activity_id_used_if_parent_id_missing(self) -> None:
        summary = base_summary()
        del summary["parentId"]
        summary["parentActivityId"] = 888
        context = build_activity_context(summary)
        assert context.parent_activity_id == 888

    def test_no_parent_stays_none(self) -> None:
        context = build_activity_context(base_summary(parentId=None))
        assert context.parent_activity_id is None


class TestTzOffset:
    def test_positive_offset_two_hours(self) -> None:
        context = build_activity_context(
            base_summary(startTimeLocal="2026-06-01 19:00:00", startTimeGMT="2026-06-01 17:00:00")
        )
        assert context.tz_offset_s == 7200
        assert context.start_time_local == datetime(2026, 6, 1, 19, 0, 0)

    def test_negative_offset(self) -> None:
        # Timezone west of GMT (e.g. UTC-5): local time is BEFORE GMT.
        context = build_activity_context(
            base_summary(startTimeLocal="2026-06-01 12:00:00", startTimeGMT="2026-06-01 17:00:00")
        )
        assert context.tz_offset_s == -18000

    def test_offset_summertime_one_hour_thirty(self) -> None:
        # Case UTC+1:30 (e.g. some non-round timezones): verifies we never assume an
        # offset multiple of a round hour.
        context = build_activity_context(
            base_summary(startTimeLocal="2026-06-01 18:30:00", startTimeGMT="2026-06-01 17:00:00")
        )
        assert context.tz_offset_s == 5400

    def test_start_time_local_missing_raises_value_error(self) -> None:
        summary = base_summary()
        del summary["startTimeLocal"]
        with pytest.raises(ValueError, match="startTimeLocal"):
            build_activity_context(summary)

    def test_start_time_gmt_missing_raises_value_error(self) -> None:
        summary = base_summary()
        del summary["startTimeGMT"]
        with pytest.raises(ValueError, match="startTimeGMT"):
            build_activity_context(summary)

    def test_start_time_local_malformed_raises_value_error(self) -> None:
        summary = base_summary(startTimeLocal="01/06/2026 19h00")
        with pytest.raises(ValueError, match="startTimeLocal"):
            build_activity_context(summary)


class TestDuration:
    def test_zero_duration_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            build_activity_context(base_summary(duration=0.0))

    def test_negative_duration_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            build_activity_context(base_summary(duration=-10.0))

    def test_missing_duration_raises_value_error(self) -> None:
        summary = base_summary()
        del summary["duration"]
        with pytest.raises(ValueError, match="duration"):
            build_activity_context(summary)

    def test_duration_becomes_positive_int(self) -> None:
        context = build_activity_context(base_summary(duration=3600.7))
        assert context.duration_s == 3601
        assert isinstance(context.duration_s, int)


class TestOptionalFieldsMissing:
    def test_missing_optional_fields_stay_none_never_zero(self) -> None:
        summary = base_summary()
        for key in (
            "elapsedDuration",
            "movingDuration",
            "distance",
            "elevationGain",
            "elevationLoss",
            "calories",
            "activityName",
        ):
            del summary[key]

        context = build_activity_context(summary)

        assert context.elapsed_s is None
        assert context.moving_s is None
        assert context.distance_m is None
        assert context.ascent_m is None
        assert context.descent_m is None
        assert context.calories is None
        assert context.name is None

    def test_present_optional_fields_are_carried(self) -> None:
        context = build_activity_context(base_summary())
        assert context.elapsed_s == 3650
        assert context.moving_s == 3580
        assert context.distance_m == 10000.0
        assert context.ascent_m == 120.0
        assert context.descent_m == 115.0
        assert context.calories == 650
        assert context.name == "Evening ride"


class TestSha256:
    def test_summary_sha256_matches_canonical_payload_hash(self) -> None:
        summary = base_summary()
        context = build_activity_context(summary)
        assert context.summary_sha256 == payload_sha256(summary)

    def test_two_different_summaries_have_different_hashes(self) -> None:
        c1 = build_activity_context(base_summary(activityName="A"))
        c2 = build_activity_context(base_summary(activityName="B"))
        assert c1.summary_sha256 != c2.summary_sha256


class TestActivityIdAndTypeKey:
    def test_missing_activity_id_raises_value_error(self) -> None:
        summary = base_summary()
        del summary["activityId"]
        with pytest.raises(ValueError, match="activityId"):
            build_activity_context(summary)

    def test_missing_type_key_raises_value_error(self) -> None:
        summary = base_summary(activityType={})
        with pytest.raises(ValueError, match="typeKey"):
            build_activity_context(summary)

    def test_garmin_type_key_is_preserved_on_context(self) -> None:
        context = build_activity_context(base_summary())
        assert context.garmin_type_key == "running"


# -- load_thresholds: fake in-memory connection, no real PostgreSQL -----------------


class _FakeCursor:
    """Simulates just enough of the psycopg 3 API for ``load_thresholds``: a fixed query,
    results already "flattened" as ``lower()``/``upper()`` would in SQL."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, query: str, params: Any = ()) -> None:
        self._last_params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _FakeThresholdDB:
    """In-memory connection: rows are already in the format that
    ``SELECT threshold_id, discipline, lower(validity), upper(validity), ...`` would return."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.commit_count = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        self.commit_count += 1


def _threshold_row(
    threshold_id: int,
    discipline: str,
    valid_from: date,
    valid_to: date | None,
    **extra: Any,
) -> tuple[Any, ...]:
    columns = {
        "ftp_watts": None,
        "rftp_watts": None,
        "ftp_pace_s_per_km": None,
        "css_m_s": None,
        "lthr_bpm": None,
        "hr_max_bpm": None,
        "hr_rest_bpm": None,
    }
    columns.update(extra)
    return (
        threshold_id,
        discipline,
        valid_from,
        valid_to,
        columns["ftp_watts"],
        columns["rftp_watts"],
        columns["ftp_pace_s_per_km"],
        columns["css_m_s"],
        columns["lthr_bpm"],
        columns["hr_max_bpm"],
        columns["hr_rest_bpm"],
    )


class TestLoadThresholds:
    def test_empty_list_without_rows(self) -> None:
        db = _FakeThresholdDB([])
        assert load_thresholds(db, athlete_id=1) == ()

    def test_builds_thresholds_from_lower_upper_bounds(self) -> None:
        rows = [
            _threshold_row(1, "bike", date(2026, 1, 1), None, ftp_watts=220.0),
            _threshold_row(
                2,
                "run",
                date(2026, 1, 1),
                date(2026, 6, 1),
                ftp_pace_s_per_km=270.0,
            ),
        ]
        db = _FakeThresholdDB(rows)

        thresholds = load_thresholds(db, athlete_id=1)

        assert len(thresholds) == 2
        bike = thresholds[0]
        assert bike.threshold_id == 1
        assert bike.discipline == "bike"
        assert bike.valid_from == date(2026, 1, 1)
        assert bike.valid_to is None  # upper bound NULL = current threshold.
        assert bike.ftp_watts == 220.0
        run = thresholds[1]
        assert run.valid_to == date(2026, 6, 1)
        assert run.ftp_pace_s_per_km == 270.0


class TestSelectThresholds:
    def test_covered_date_returns_both_thresholds(self) -> None:
        rows = [
            _threshold_row(1, "bike", date(2026, 1, 1), None, ftp_watts=220.0),
            _threshold_row(2, "all", date(2026, 1, 1), None, hr_max_bpm=190, hr_rest_bpm=55),
        ]
        thresholds = load_thresholds(_FakeThresholdDB(rows), athlete_id=1)

        threshold, all_threshold = select_thresholds(thresholds, "bike", date(2026, 6, 1))

        assert threshold is not None
        assert threshold.ftp_watts == 220.0
        assert all_threshold is not None
        assert all_threshold.hr_max_bpm == 190

    def test_uncovered_date_returns_none_without_raising(self) -> None:
        rows = [
            _threshold_row(1, "bike", date(2026, 1, 1), None, ftp_watts=220.0),
        ]
        thresholds = load_thresholds(_FakeThresholdDB(rows), athlete_id=1)

        # Activity before any known validity period: None, no exception
        # (explicit business rule of garminator.load.thresholds.threshold_for_date).
        threshold, all_threshold = select_thresholds(thresholds, "bike", date(2025, 1, 1))

        assert threshold is None
        assert all_threshold is None


def test_floating_point_durations_are_rounded_not_truncated() -> None:
    """Guarantee of CONSISTENCY between duration_s and elapsed_s/moving_s.

    Garmin returns floating-point seconds. Since ``duration_s`` is rounded, truncating
    ``elapsedDuration`` would produce elapsed time less than session time -- a
    physically impossible state, caused only by two divergent conversions.
    """
    context = build_activity_context(
        {
            "activityId": 4242,
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-04-02 08:00:00",
            "startTimeGMT": "2026-04-02 06:00:00",
            "duration": 3599.6,
            "elapsedDuration": 3599.6,
            "movingDuration": 3599.6,
        }
    )
    assert context.duration_s == 3600
    assert context.elapsed_s == 3600
    assert context.moving_s == 3600
    assert context.elapsed_s >= context.duration_s
