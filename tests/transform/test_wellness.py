"""Tests for :mod:`t0.transform.wellness`, the pure extraction layer.

Every payload below is HAND-WRITTEN and synthetic: no byte of the athlete's real exports
appears here (they carry GPS traces identifying a home address, cf. the layer mandate).
The shapes -- and only the shapes -- reproduce what Garmin actually serves: the two
descriptor spellings, the ``-1`` stress sentinel, ``sleepLevels: null``, weights in grams,
``get_training_readiness`` answering an empty list.

Each test pins one rule that, if broken, would corrupt the dashboards SILENTLY: a wrong
column read as a value, a sentinel stored as a score, an offset applied twice. None of
those raise -- they just produce plausible, wrong numbers -- which is exactly why they are
tested here rather than left to the runtime.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from t0.transform.wellness import (
    METRIC_ID_BODY_BATTERY,
    METRIC_ID_HRV,
    METRIC_ID_STRESS,
    extract_body_battery,
    extract_floors,
    extract_hrv,
    extract_intensity_minutes,
    extract_resting_hr,
    extract_sleep,
    extract_steps,
    extract_stress,
    extract_training_readiness,
    extract_vo2max,
    extract_weight_kg,
)

ATHLETE_ID = 7
LOCAL_DATE = date(2026, 6, 1)

#: 2026-06-01T00:00:00Z, in milliseconds -- the base every synthetic timestamp offsets from.
MIDNIGHT_MS = 1780272000000
MINUTE_MS = 60_000


def _at(minutes: int) -> int:
    return MIDNIGHT_MS + minutes * MINUTE_MS


def _utc(minutes: int) -> datetime:
    return datetime.fromtimestamp(_at(minutes) / 1000.0, tz=UTC)


# -- sleep ------------------------------------------------------------------------------


def _sleep_payload(**dto_overrides: object) -> dict[str, object]:
    """A complete, nominal night: asleep 22:00 the day before, awake 06:00 on LOCAL_DATE."""
    dto: dict[str, object] = {
        "calendarDate": "2026-06-01",
        "sleepStartTimestampGMT": _at(-120),
        "sleepEndTimestampGMT": _at(360),
        # Local = same instant already shifted by +2h, cf. _epoch_ms_to_naive_local.
        "sleepStartTimestampLocal": _at(0),
        "sleepTimeSeconds": 28800,
        "deepSleepSeconds": 3600,
        "lightSleepSeconds": 18000,
        "remSleepSeconds": 5400,
        "awakeSleepSeconds": 1800,
        "sleepScores": {"overall": {"value": 82}},
        "averageSpO2Value": 95.0,
        "averageRespirationValue": 13.5,
    }
    dto.update(dto_overrides)
    return {"dailySleepDTO": dto, "sleepLevels": None}


def test_sleep_session_is_keyed_by_the_wake_date_from_calendar_date() -> None:
    session, _ = extract_sleep(_sleep_payload(), athlete_id=ATHLETE_ID)

    assert session is not None
    # The night STARTS on 05-31 and must still be filed under 06-01: core.sleep_session is
    # keyed by wake date, and deriving it from sleep_start would shift every night by a day.
    assert session.wake_date == date(2026, 6, 1)
    assert session.sleep_start == _utc(-120)
    assert session.sleep_end == _utc(360)
    assert session.duration_s == 28800
    assert session.sleep_score == 82


def test_local_start_is_naive_and_not_shifted_twice() -> None:
    session, _ = extract_sleep(_sleep_payload(), athlete_id=ATHLETE_ID)

    assert session is not None
    # Same wall-clock reading as the UTC interpretation, tzinfo stripped. Converting instead
    # of stripping would move it by the offset a second time.
    assert session.sleep_start_local == _utc(0).replace(tzinfo=None)
    assert session.sleep_start_local.tzinfo is None


def test_missing_duration_falls_back_to_the_measured_span() -> None:
    payload: dict[str, Any] = _sleep_payload()
    del payload["dailySleepDTO"]["sleepTimeSeconds"]

    session, _ = extract_sleep(payload, athlete_id=ATHLETE_ID)

    assert session is not None
    assert session.duration_s == 480 * 60


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"sleepStartTimestampGMT": None}, "watch not worn: no start"),
        ({"sleepEndTimestampGMT": None}, "watch not worn: no end"),
        ({"sleepEndTimestampGMT": _at(-120)}, "zero-length night, rejected by the CHECK"),
        ({"sleepEndTimestampGMT": _at(-300)}, "end before start"),
    ],
)
def test_an_absent_night_produces_no_row(overrides: dict[str, object], reason: str) -> None:
    session, stages = extract_sleep(_sleep_payload(**overrides), athlete_id=ATHLETE_ID)

    assert session is None, reason
    assert stages == []


def test_sleep_levels_null_yields_no_stage_but_keeps_the_session() -> None:
    session, stages = extract_sleep(_sleep_payload(), athlete_id=ATHLETE_ID)

    # sleepLevels is null in the fixture: Garmin does return that on complete nights, and it
    # must cost the phases only -- never the summary the dashboard reads.
    assert session is not None
    assert stages == []


def test_activity_levels_map_to_the_four_stage_names() -> None:
    payload = _sleep_payload()
    payload["sleepLevels"] = [
        {
            "startGMT": "2026-05-31T22:00:00.0",
            "endGMT": "2026-05-31T23:00:00.0",
            "activityLevel": 0,
        },
        {
            "startGMT": "2026-05-31T23:00:00.0",
            "endGMT": "2026-06-01T03:00:00.0",
            "activityLevel": 1,
        },
        {
            "startGMT": "2026-06-01T03:00:00.0",
            "endGMT": "2026-06-01T05:00:00.0",
            "activityLevel": 2,
        },
        {
            "startGMT": "2026-06-01T05:00:00.0",
            "endGMT": "2026-06-01T06:00:00.0",
            "activityLevel": 3,
        },
    ]

    _, stages = extract_sleep(payload, athlete_id=ATHLETE_ID)

    assert [stage.stage for stage in stages] == ["deep", "light", "rem", "awake"]
    # The *GMT fields carry no offset; leaving them naive would let the server reinterpret
    # them in its own timezone.
    assert stages[0].stage_start == datetime(2026, 5, 31, 22, 0, tzinfo=UTC)
    assert all(stage.wake_date == date(2026, 6, 1) for stage in stages)


def test_duplicate_and_empty_segments_are_skipped() -> None:
    payload = _sleep_payload()
    payload["sleepLevels"] = [
        {
            "startGMT": "2026-05-31T22:00:00.0",
            "endGMT": "2026-05-31T23:00:00.0",
            "activityLevel": 0,
        },
        # Same start: the PK is (athlete_id, wake_date, stage_start), so keeping it would
        # abort the insert of the WHOLE night rather than lose one segment.
        {
            "startGMT": "2026-05-31T22:00:00.0",
            "endGMT": "2026-06-01T01:00:00.0",
            "activityLevel": 1,
        },
        {
            "startGMT": "2026-06-01T02:00:00.0",
            "endGMT": "2026-06-01T02:00:00.0",
            "activityLevel": 1,
        },
        {
            "startGMT": "2026-06-01T03:00:00.0",
            "endGMT": "2026-06-01T04:00:00.0",
            "activityLevel": 9,
        },
    ]

    _, stages = extract_sleep(payload, athlete_id=ATHLETE_ID)

    # Only the first survives: duplicate start, zero-length, unknown activity level.
    assert len(stages) == 1
    assert stages[0].stage == "deep"


# -- HRV --------------------------------------------------------------------------------


def test_hrv_summary_and_readings() -> None:
    payload = {
        "hrvSummary": {"lastNightAvg": 62.0, "status": "BALANCED"},
        "hrvReadings": [
            {"readingTimeGMT": "2026-05-31T23:05:00.0", "hrvValue": 58},
            {"readingTimeGMT": "2026-06-01T01:05:00.0", "hrvValue": 66},
            {"readingTimeGMT": "2026-06-01T01:05:00.0", "hrvValue": 70},
            {"readingTimeGMT": "2026-06-01T02:05:00.0", "hrvValue": None},
        ],
    }

    avg, status, samples = extract_hrv(payload, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    assert avg == 62.0
    assert status == "BALANCED"
    # Duplicate instant dropped (PK is (metric_id, t)), null reading dropped.
    assert [sample.value for sample in samples] == [58.0, 66.0]
    assert all(sample.metric_id == METRIC_ID_HRV for sample in samples)
    assert all(sample.local_date == LOCAL_DATE for sample in samples)


def test_hrv_missing_summary_stays_none() -> None:
    avg, status, samples = extract_hrv({}, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    assert (avg, status, samples) == (None, None, [])


# -- resting HR -------------------------------------------------------------------------


def test_resting_hr_selects_the_requested_day_not_the_first_entry() -> None:
    payload = {
        "allMetrics": {
            "metricsMap": {
                "WELLNESS_RESTING_HEART_RATE": [
                    {"calendarDate": "2026-05-30", "value": 55},
                    {"calendarDate": "2026-06-01", "value": 48},
                ]
            }
        }
    }

    assert extract_resting_hr(payload, local_date=LOCAL_DATE) == 48


def test_resting_hr_absent_day_stays_none() -> None:
    payload: dict[str, Any] = {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": []}}}

    assert extract_resting_hr(payload, local_date=LOCAL_DATE) is None
    assert extract_resting_hr({}, local_date=LOCAL_DATE) is None


# -- stress -----------------------------------------------------------------------------


def test_stress_reads_its_index_from_the_descriptors_and_drops_the_sentinel() -> None:
    payload = {
        "avgStressLevel": 31,
        # Under get_stress_data the value sits at index 2, NOT 1.
        "stressValueDescriptorsDTOList": [
            {"key": "timestamp", "index": 0},
            {"key": "bodyBatteryLevel", "index": 1},
            {"key": "stressLevel", "index": 2},
        ],
        "stressValuesArray": [
            [_at(0), 77, 25],
            [_at(3), 76, -1],
            [_at(6), 75, 40],
        ],
    }

    avg, samples = extract_stress(payload, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    assert avg == 31
    # 25 and 40, never 77/76/75 (that column is Body Battery) and never -1.
    assert [sample.value for sample in samples] == [25.0, 40.0]
    assert all(sample.metric_id == METRIC_ID_STRESS for sample in samples)


def test_unknown_descriptor_layout_drops_the_series_rather_than_guessing() -> None:
    payload = {
        "avgStressLevel": 31,
        "stressValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}],
        "stressValuesArray": [[_at(0), 25]],
    }

    avg, samples = extract_stress(payload, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    # The average survives; the series does not, because no index says where it lives.
    assert avg == 31
    assert samples == []


# -- body battery -----------------------------------------------------------------------


def test_body_battery_reads_the_other_descriptor_spelling_and_computes_extremes() -> None:
    payload = [
        {
            # get_body_battery uses bodyBatteryValueDescriptor{Key,Index} and index 1.
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
            "charged": 60,
            "drained": 44,
            "bodyBatteryValuesArray": [[_at(0), 88], [_at(180), 41], [_at(360), 55]],
        }
    ]

    high, low, samples = extract_body_battery(payload, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    # From the retained points -- charged/drained are totals, not extremes.
    assert (high, low) == (88, 41)
    assert len(samples) == 3
    assert all(sample.metric_id == METRIC_ID_BODY_BATTERY for sample in samples)


def test_body_battery_without_points_stays_none() -> None:
    high, low, samples = extract_body_battery([], athlete_id=ATHLETE_ID, local_date=LOCAL_DATE)

    assert (high, low, samples) == (None, None, [])


# -- VO2max, weight, readiness ----------------------------------------------------------


def test_vo2max_prefers_the_precise_value_and_never_backfills_cycling() -> None:
    payload = {
        "mostRecentVO2Max": {
            "generic": {"vo2MaxValue": 58.0, "vo2MaxPreciseValue": 58.4},
            "cycling": None,
        }
    }

    running, cycling = extract_vo2max(payload)

    # cycling stays None: `generic` is the RUNNING estimate under a neutral name.
    assert running == 58.4
    assert cycling is None


def test_vo2max_absent_stays_none() -> None:
    assert extract_vo2max({}) == (None, None)


def test_weight_is_converted_from_grams_to_kilograms() -> None:
    payload = {"dateWeightList": [{"calendarDate": "2026-06-01", "weight": 74500.0}]}

    assert extract_weight_kg(payload, local_date=LOCAL_DATE) == 74.5


def test_weight_without_a_scale_stays_none() -> None:
    # The real case for this athlete: dateWeightList comes back empty on every payload.
    assert extract_weight_kg({"dateWeightList": []}, local_date=LOCAL_DATE) is None
    assert extract_weight_kg({}, local_date=LOCAL_DATE) is None


def test_steps_uses_the_authoritative_daily_total() -> None:
    assert extract_steps({"totalSteps": 12345, "stepsList": [{"steps": 10}]}) == 12345


def test_steps_sums_interval_records() -> None:
    assert extract_steps([{"steps": 100}, {"steps": 250}, {"steps": 0}]) == 350


def test_missing_or_negative_steps_stay_none() -> None:
    assert extract_steps({}) is None
    assert extract_steps({"totalSteps": -1}) is None


# -- floors -----------------------------------------------------------------------------


def _floors_payload(*buckets: tuple[int, int]) -> dict[str, object]:
    """A get_floors response: 15-minute buckets plus the self-describing column map."""
    return {
        "floorValuesArray": [
            ["2026-06-01T00:00:00.0", "2026-06-01T00:15:00.0", ascended, descended]
            for ascended, descended in buckets
        ],
        "floorsValueDescriptorDTOList": [
            {"key": "startTimeGMT", "index": 0},
            {"key": "endTimeGMT", "index": 1},
            {"key": "floorsAscended", "index": 2},
            {"key": "floorsDescended", "index": 3},
        ],
    }


def test_floors_sums_ascent_only() -> None:
    # Descent must not be added back: the column counts floors CLIMBED, and every staircase
    # that is walked back down would otherwise be counted twice.
    assert extract_floors(_floors_payload((3, 3), (0, 0), (5, 2))) == 8


def test_floors_follows_the_payload_column_map() -> None:
    # A Garmin-side column reorder must move the reader, not swap ascent and descent.
    payload = _floors_payload((3, 9))
    payload["floorsValueDescriptorDTOList"] = [
        {"key": "startTimeGMT", "index": 0},
        {"key": "endTimeGMT", "index": 1},
        {"key": "floorsDescended", "index": 2},
        {"key": "floorsAscended", "index": 3},
    ]

    assert extract_floors(payload) == 9


def test_a_day_of_empty_buckets_is_zero_not_none() -> None:
    # The real case since 2026-08-04: the buckets keep arriving, the barometer reports
    # nothing. That is a measured zero, and it must not be confused with an absent day.
    assert extract_floors(_floors_payload((0, 0), (0, 0))) == 0
    assert extract_floors({}) is None
    assert extract_floors([]) is None


# -- intensity minutes ------------------------------------------------------------------


def test_intensity_minutes_counts_a_vigorous_minute_twice() -> None:
    # Garmin's own weighting, cross-checked against the weekly running totals the same
    # payload carries: 18 + 2 * 31 = 80 = endDayMinutes 125 - startDayMinutes 45.
    payload = {
        "moderateMinutes": 18,
        "vigorousMinutes": 31,
        "startDayMinutes": 45,
        "endDayMinutes": 125,
    }

    assert extract_intensity_minutes(payload) == 80


def test_a_day_with_only_one_intensity_band_still_counts() -> None:
    assert extract_intensity_minutes({"moderateMinutes": 12}) == 12
    assert extract_intensity_minutes({"vigorousMinutes": 12}) == 24
    assert extract_intensity_minutes({"moderateMinutes": 0, "vigorousMinutes": 0}) == 0


def test_missing_intensity_minutes_stay_none() -> None:
    assert extract_intensity_minutes({"weeklyTotal": 125}) is None
    assert extract_intensity_minutes([]) is None


def test_training_readiness_empty_list_stays_none() -> None:
    # A Forerunner 255 answers [] on every day: the column must stay NULL, never become 0.
    assert extract_training_readiness([]) is None
    assert extract_training_readiness([{"score": 71}]) == 71


def test_the_sentinel_is_dropped_from_the_daily_average_too() -> None:
    # Observed on a real day the watch was off the wrist: Garmin answers -1 for the whole
    # day's average, not only for the minutes it could not measure.
    avg, samples = extract_stress(
        {"avgStressLevel": -1}, athlete_id=ATHLETE_ID, local_date=LOCAL_DATE
    )

    assert avg is None
    assert samples == []
