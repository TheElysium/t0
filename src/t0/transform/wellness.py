"""Extract ``core.daily_wellness`` / ``core.sleep_session`` / ``core.sleep_stage`` /
``core.wellness_sample`` rows from the wellness JSON payloads held in ``raw.api_response``.

WHY THIS MODULE EXISTS AT ALL. :mod:`t0.transform.runner` is FILE-driven: it walks
``raw.fit_file`` and produces activities. The wellness endpoints archive no file -- their
whole content is the JSON payload -- so before this module nothing ever read them and
``core.daily_wellness`` stayed empty however many times the scheduler ran.

Every function here is PURE: a decoded payload in, dataclasses out, no connection, no
clock, no I/O. Writing lives in :mod:`t0.transform.wellness_store`, orchestration
in :mod:`t0.transform.wellness_runner`. Same split as ``activity.py`` /
``store.py`` / ``runner.py``, for the same reason: the extraction rules are where the
subtle mistakes live, and they must be testable without PostgreSQL.

THREE RULES THIS MODULE NEVER BREAKS.

1. **A column index is resolved from the payload's own descriptor list, never hardcoded.**
   Garmin describes each "values array" with its own descriptor list, under two different
   SPELLINGS (``{"key", "index"}`` for stress, ``bodyBatteryValueDescriptor{Key,Index}``
   for Body Battery), and the same endpoint does not always ship the same columns: on the
   athlete's 31 polled days ``get_stress_data`` carried only ``timestamp`` and
   ``stressLevel``, but the endpoint is documented elsewhere as also carrying
   ``bodyBatteryLevel``. A hardcoded index reads whichever column happens to sit there and
   stores it as a value -- silently, since every column here is a plausible small integer.
2. **A sentinel is dropped, never stored.** Garmin encodes "not measurable" as
   ``stressLevel = -1``; ``core.wellness_sample.value`` is ``NOT NULL``, so those points
   must be absent -- never written as -1 (which reads as a real score) nor as 0 (which
   reads as perfect calm).
3. **A missing field stays ``None``.** Every ``core.daily_wellness`` column is nullable
   precisely so a device that does not measure something leaves a hole rather than a
   fabricated zero -- the Forerunner 255 never returns ``training_readiness`` at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: ``core.wellness_metric.metric_id`` values seeded by migration 006. Referenced by code
#: rather than re-queried per pass: the table is a frozen 9-row reference seeded in the
#: same migration that creates it, and a join per payload would buy nothing.
METRIC_ID_STRESS = 2
METRIC_ID_BODY_BATTERY = 3
METRIC_ID_HRV = 7

#: ``sleepLevels[].activityLevel`` -> ``core.sleep_stage.stage``.
#:
#: NOT taken from documentation -- Garmin publishes none for this field. VERIFIED against
#: the athlete's own nights by summing each level's segment durations and comparing to the
#: ``dailySleepDTO`` totals Garmin computes itself: level 0 matched ``deepSleepSeconds`` on
#: 25/25 nights, level 1 ``lightSleepSeconds`` 25/25, level 2 ``remSleepSeconds`` 24/25,
#: level 3 ``awakeSleepSeconds`` 23/25 (the odd night differs by Garmin's own rounding of
#: nap segments, not by a mislabelled phase).
SLEEP_STAGE_BY_ACTIVITY_LEVEL: dict[int, str] = {0: "deep", 1: "light", 2: "rem", 3: "awake"}

#: Below this, ``stressLevel`` is Garmin's "not measurable" sentinel rather than a score.
_STRESS_UNMEASURABLE_BELOW = 0


@dataclass(frozen=True, slots=True)
class WellnessSampleRow:
    """One row of ``core.wellness_sample`` (long format, migration 006)."""

    metric_id: int
    t: datetime
    athlete_id: int
    value: float
    local_date: date


@dataclass(frozen=True, slots=True)
class SleepStageRow:
    """One row of ``core.sleep_stage``: a phase as an INTERVAL, not a per-minute sample."""

    athlete_id: int
    wake_date: date
    stage_start: datetime
    stage_end: datetime
    stage: str


@dataclass(frozen=True, slots=True)
class SleepSessionRow:
    """One row of ``core.sleep_session``, keyed by WAKE date (cf. migration 006)."""

    athlete_id: int
    wake_date: date
    sleep_start: datetime
    sleep_end: datetime
    sleep_start_local: datetime
    duration_s: int
    deep_s: int | None
    light_s: int | None
    rem_s: int | None
    awake_s: int | None
    sleep_score: int | None
    avg_spo2: float | None
    avg_respiration: float | None
    source_sha256: bytes | None


@dataclass(frozen=True, slots=True)
class DailyWellnessRow:
    """One row of ``core.daily_wellness``: Garmin's OWN daily aggregates, never recomputed
    from :class:`WellnessSampleRow` -- the two would drift and there would be no way to
    tell which one the watch actually displayed."""

    athlete_id: int
    local_date: date
    resting_hr: int | None = None
    hrv_last_night_avg: float | None = None
    hrv_status: str | None = None
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    avg_stress: int | None = None
    steps: int | None = None
    floors: int | None = None
    intensity_minutes: int | None = None
    weight_kg: float | None = None
    training_readiness: int | None = None
    vo2max_running: float | None = None
    vo2max_cycling: float | None = None


# -- primitives ------------------------------------------------------------------------


def _epoch_ms_to_utc(value: Any) -> datetime | None:
    """Garmin timestamps are milliseconds since the Unix epoch, UTC."""
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def _epoch_ms_to_naive_local(value: Any) -> datetime | None:
    """``*TimestampLocal`` fields are the SAME instant already shifted to the athlete's
    offset, so they must be read as UTC and then stripped of their tzinfo -- treating them
    as real UTC instants would double-apply the offset."""
    moment = _epoch_ms_to_utc(value)
    return None if moment is None else moment.replace(tzinfo=None)


def _as_int(value: Any) -> int | None:
    return None if value is None else round(float(value))


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _parse_gmt_text(value: Any) -> datetime | None:
    """``sleepLevels`` and ``hrvReadings`` timestamps are ISO TEXT carrying no offset
    (``2026-08-01T21:47:00.0``) even though the fields are named ``*GMT``.

    UTC is attached explicitly here rather than leaving the value naive: the target columns
    are ``timestamptz``, and a naive value would be silently reinterpreted in the server's
    timezone -- a one- or two-hour shift that no test on a UTC machine would ever catch.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _descriptor_index(descriptors: Any, key: str) -> int | None:
    """Locate a column in a Garmin "values array" from its descriptor list.

    Garmin ships two descriptor spellings -- ``{"key", "index"}`` under ``get_stress_data``
    and ``{"bodyBatteryValueDescriptorKey", "bodyBatteryValueDescriptorIndex"}`` under
    ``get_body_battery`` -- for series carrying the SAME metric at DIFFERENT positions. Both
    spellings are read here; returning ``None`` (rather than guessing 1) is what makes an
    unrecognized layout drop the series instead of storing a wrong column as a value.
    """
    if not isinstance(descriptors, list):
        return None
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        name = descriptor.get("key", descriptor.get("bodyBatteryValueDescriptorKey"))
        if name != key:
            continue
        index = descriptor.get("index", descriptor.get("bodyBatteryValueDescriptorIndex"))
        return None if index is None else int(index)
    return None


def _iter_values_array(values: Any, descriptors: Any, key: str) -> Iterator[tuple[datetime, float]]:
    """Yield ``(instant, value)`` for a Garmin ``*ValuesArray``, skipping any row whose
    timestamp or value is absent: ``None`` inside such an array means "no reading at this
    minute", which is an absence, not a zero."""
    timestamp_index = _descriptor_index(descriptors, "timestamp")
    value_index = _descriptor_index(descriptors, key)
    if timestamp_index is None or value_index is None or not isinstance(values, list):
        return
    for row in values:
        if not isinstance(row, list) or len(row) <= max(timestamp_index, value_index):
            continue
        moment = _epoch_ms_to_utc(row[timestamp_index])
        value = row[value_index]
        if moment is None or value is None:
            continue
        yield moment, float(value)


# -- per-endpoint extraction ------------------------------------------------------------


def extract_sleep(
    payload: dict[str, Any],
    *,
    athlete_id: int,
    source_sha256: bytes | None = None,
) -> tuple[SleepSessionRow | None, list[SleepStageRow]]:
    """``get_sleep_data`` -> one night plus its phases, or ``(None, [])``.

    A day the watch was not worn still returns a well-formed payload whose ``dailySleepDTO``
    carries no timestamps: that is an absent night, not an error, and it must produce no row
    at all -- ``core.sleep_session`` declares ``CHECK (sleep_end > sleep_start)``, so a
    zero-length night would be rejected by the database anyway.
    """
    dto = payload.get("dailySleepDTO")
    if not isinstance(dto, dict):
        return None, []
    calendar_date = dto.get("calendarDate")
    sleep_start = _epoch_ms_to_utc(dto.get("sleepStartTimestampGMT"))
    sleep_end = _epoch_ms_to_utc(dto.get("sleepEndTimestampGMT"))
    start_local = _epoch_ms_to_naive_local(dto.get("sleepStartTimestampLocal"))
    if calendar_date is None or sleep_start is None or sleep_end is None or start_local is None:
        return None, []
    if sleep_end <= sleep_start:
        return None, []

    # WAKE date, per migration 006: get_sleep_data(d) returns the night that ENDS on d, and
    # `calendarDate` echoes exactly the requested day (verified 31/31 on real payloads).
    wake_date = date.fromisoformat(str(calendar_date))
    duration_s = _as_int(dto.get("sleepTimeSeconds"))
    if duration_s is None:
        duration_s = round((sleep_end - sleep_start).total_seconds())

    scores = dto.get("sleepScores")
    overall = scores.get("overall") if isinstance(scores, dict) else None
    sleep_score = _as_int(overall.get("value")) if isinstance(overall, dict) else None

    session = SleepSessionRow(
        athlete_id=athlete_id,
        wake_date=wake_date,
        sleep_start=sleep_start,
        sleep_end=sleep_end,
        sleep_start_local=start_local,
        duration_s=duration_s,
        deep_s=_as_int(dto.get("deepSleepSeconds")),
        light_s=_as_int(dto.get("lightSleepSeconds")),
        rem_s=_as_int(dto.get("remSleepSeconds")),
        awake_s=_as_int(dto.get("awakeSleepSeconds")),
        sleep_score=sleep_score,
        avg_spo2=_as_float(dto.get("averageSpO2Value")),
        avg_respiration=_as_float(dto.get("averageRespirationValue")),
        source_sha256=source_sha256,
    )
    return session, _extract_sleep_stages(payload, athlete_id=athlete_id, wake_date=wake_date)


def _extract_sleep_stages(
    payload: dict[str, Any], *, athlete_id: int, wake_date: date
) -> list[SleepStageRow]:
    """``sleepLevels`` -> phase intervals.

    ``sleepLevels`` is ``null`` on some nights even when the session itself is complete
    (observed on the athlete's real data), hence the type guard rather than a bare loop.
    """
    levels = payload.get("sleepLevels")
    if not isinstance(levels, list):
        return []
    stages: list[SleepStageRow] = []
    seen_starts: set[datetime] = set()
    for level in levels:
        if not isinstance(level, dict):
            continue
        activity_level = level.get("activityLevel")
        stage = (
            SLEEP_STAGE_BY_ACTIVITY_LEVEL.get(round(float(activity_level)))
            if activity_level is not None
            else None
        )
        stage_start = _parse_gmt_text(level.get("startGMT"))
        stage_end = _parse_gmt_text(level.get("endGMT"))
        if stage is None or stage_start is None or stage_end is None:
            continue
        if stage_end <= stage_start or stage_start in seen_starts:
            # The primary key is (athlete_id, wake_date, stage_start): a duplicated start
            # would abort the whole night's insert rather than lose a single segment.
            continue
        seen_starts.add(stage_start)
        stages.append(
            SleepStageRow(
                athlete_id=athlete_id,
                wake_date=wake_date,
                stage_start=stage_start,
                stage_end=stage_end,
                stage=stage,
            )
        )
    return stages


def extract_hrv(
    payload: dict[str, Any], *, athlete_id: int, local_date: date
) -> tuple[float | None, str | None, list[WellnessSampleRow]]:
    """``get_hrv_data`` -> (last-night average, status, intraday readings)."""
    summary = payload.get("hrvSummary")
    last_night_avg: float | None = None
    status: str | None = None
    if isinstance(summary, dict):
        last_night_avg = _as_float(summary.get("lastNightAvg"))
        raw_status = summary.get("status")
        status = None if raw_status is None else str(raw_status)

    samples: list[WellnessSampleRow] = []
    readings = payload.get("hrvReadings")
    seen: set[datetime] = set()
    if isinstance(readings, list):
        for reading in readings:
            if not isinstance(reading, dict):
                continue
            moment = _parse_gmt_text(reading.get("readingTimeGMT"))
            value = _as_float(reading.get("hrvValue"))
            if moment is None or value is None or moment in seen:
                continue
            seen.add(moment)
            samples.append(
                WellnessSampleRow(
                    metric_id=METRIC_ID_HRV,
                    t=moment,
                    athlete_id=athlete_id,
                    value=value,
                    local_date=local_date,
                )
            )
    return last_night_avg, status, samples


def extract_resting_hr(payload: dict[str, Any], *, local_date: date) -> int | None:
    """``get_rhr_day`` -> the resting HR of ``local_date``.

    The endpoint answers with a whole statistics RANGE, not a single day: the entry whose
    ``calendarDate`` matches is selected explicitly rather than taking ``[0]``, which would
    attribute a neighbouring day's value on any range Garmin decides to widen.
    """
    metrics = payload.get("allMetrics")
    if not isinstance(metrics, dict):
        return None
    metrics_map = metrics.get("metricsMap")
    if not isinstance(metrics_map, dict):
        return None
    entries = metrics_map.get("WELLNESS_RESTING_HEART_RATE")
    if not isinstance(entries, list):
        return None
    wanted = local_date.isoformat()
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("calendarDate")) == wanted:
            return _as_int(entry.get("value"))
    return None


def extract_stress(
    payload: dict[str, Any], *, athlete_id: int, local_date: date
) -> tuple[int | None, list[WellnessSampleRow]]:
    """``get_stress_data`` -> (daily average, intraday series).

    Points below zero are Garmin's "not measurable" sentinel and are DROPPED, cf. rule 2 of
    the module docstring.
    """
    # The sentinel reaches the DAILY average too, not just the intraday points: Garmin
    # answers avgStressLevel = -1 for a day it could not measure (observed on a day the
    # watch was off the wrist). Stored as-is it would plot as a real, absurd score.
    avg_stress = _as_int(payload.get("avgStressLevel"))
    if avg_stress is not None and avg_stress < _STRESS_UNMEASURABLE_BELOW:
        avg_stress = None
    samples: list[WellnessSampleRow] = []
    seen: set[datetime] = set()
    for moment, value in _iter_values_array(
        payload.get("stressValuesArray"),
        payload.get("stressValueDescriptorsDTOList"),
        "stressLevel",
    ):
        if value < _STRESS_UNMEASURABLE_BELOW or moment in seen:
            continue
        seen.add(moment)
        samples.append(
            WellnessSampleRow(
                metric_id=METRIC_ID_STRESS,
                t=moment,
                athlete_id=athlete_id,
                value=value,
                local_date=local_date,
            )
        )
    return avg_stress, samples


def extract_body_battery(
    payload: Any, *, athlete_id: int, local_date: date
) -> tuple[int | None, int | None, list[WellnessSampleRow]]:
    """``get_body_battery`` -> (day high, day low, intraday series).

    This endpoint answers with a LIST of daily objects, one per requested day, so the whole
    list is walked. High and low are computed from the retained points rather than read from
    ``charged``/``drained``, which are the day's total gain and loss, not its extremes.
    """
    samples: list[WellnessSampleRow] = []
    seen: set[datetime] = set()
    days = payload if isinstance(payload, list) else [payload]
    for day in days:
        if not isinstance(day, dict):
            continue
        for moment, value in _iter_values_array(
            day.get("bodyBatteryValuesArray"),
            day.get("bodyBatteryValueDescriptorDTOList"),
            "bodyBatteryLevel",
        ):
            if moment in seen:
                continue
            seen.add(moment)
            samples.append(
                WellnessSampleRow(
                    metric_id=METRIC_ID_BODY_BATTERY,
                    t=moment,
                    athlete_id=athlete_id,
                    value=value,
                    local_date=local_date,
                )
            )
    if not samples:
        return None, None, []
    values = [sample.value for sample in samples]
    return round(max(values)), round(min(values)), samples


def extract_vo2max(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    """``get_training_status`` (or ``get_max_metrics``) -> (running VO2max, cycling VO2max).

    ``cycling`` is ``null`` on every payload for this athlete -- the bike carries no power
    meter outside the home trainer -- and stays ``None`` rather than being backfilled from
    ``generic``, which is the RUNNING estimate under a neutral name.
    """
    most_recent = payload.get("mostRecentVO2Max", payload)
    if not isinstance(most_recent, dict):
        return None, None

    def _value(node: Any) -> float | None:
        if not isinstance(node, dict):
            return None
        return _as_float(node.get("vo2MaxPreciseValue", node.get("vo2MaxValue")))

    return _value(most_recent.get("generic")), _value(most_recent.get("cycling"))


def extract_weight_kg(payload: dict[str, Any], *, local_date: date) -> float | None:
    """``get_body_composition`` -> the weight of ``local_date``, in KILOGRAMS.

    Garmin reports ``weight`` in GRAMS on this endpoint while ``core.daily_wellness.weight_kg``
    is in kilograms: that conversion is the whole point of this function. Returns ``None``
    when the athlete syncs no scale, which is the case here -- ``dateWeightList`` comes back
    empty on every payload.
    """
    entries = payload.get("dateWeightList")
    if not isinstance(entries, list):
        return None
    wanted = local_date.isoformat()
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("calendarDate")) != wanted:
            continue
        grams = _as_float(entry.get("weight"))
        return None if grams is None else grams / 1000.0
    return None


def extract_steps(payload: Any) -> int | None:
    """``get_steps_data`` -> the daily total, or ``None`` when no valid data is present.

    Garmin's current response is a list of interval records, each carrying an incremental
    ``steps`` value. Some Garmin responses instead provide ``totalSteps`` or wrap the
    interval records in ``stepsList``; support all observed shapes without treating a
    partial interval list as the authoritative total when a total is available.
    """
    if isinstance(payload, dict):
        total = _as_int(payload.get("totalSteps"))
        if total is not None:
            return None if total < 0 else total
        entries = payload.get("stepsList")
        if entries is None and "steps" in payload:
            entries = [payload]
    elif isinstance(payload, list):
        entries = payload
    else:
        return None

    if not isinstance(entries, list):
        return None
    values = [
        steps
        for entry in entries
        if isinstance(entry, dict)
        for steps in [_as_int(entry.get("steps"))]
        if steps is not None and steps >= 0
    ]
    return sum(values) if values else None


def _floor_column_index(payload: dict[str, Any], key: str, fallback: int) -> int:
    """Position of ``key`` in a ``floorValuesArray`` row, read from the payload's own
    descriptor list rather than hardcoded.

    ``get_floors`` ships ``floorsValueDescriptorDTOList``, a self-describing index ->
    column map. Resolving through it means a Garmin-side column reorder produces correct
    numbers instead of silently swapping ascent and descent. ``fallback`` is the position
    observed on 2026-08-26 and applies only when the descriptor list is absent.
    """
    descriptors = payload.get("floorsValueDescriptorDTOList")
    if isinstance(descriptors, list):
        for descriptor in descriptors:
            if isinstance(descriptor, dict) and descriptor.get("key") == key:
                index = _as_int(descriptor.get("index"))
                if index is not None and index >= 0:
                    return index
    return fallback


def extract_floors(payload: Any) -> int | None:
    """``get_floors`` -> floors climbed over the day, or ``None`` when unreadable.

    The payload carries ``floorValuesArray``, one row per 15-minute bucket shaped
    ``[startTimeGMT, endTimeGMT, floorsAscended, floorsDescended]`` -- column positions
    resolved through :func:`_floor_column_index`. Only ascent is summed: the column
    records floors CLIMBED, and adding descent back would double-count every staircase.

    A payload whose buckets are all zero yields ``0``, not ``None``: a genuinely flat day
    and a day the barometer never registered are indistinguishable from the response
    alone, and the endpoint answering with a full bucket array is itself the evidence that
    the day was measured. On this athlete's history, ascent stops being reported after
    2026-08-03 while the buckets keep arriving -- which is exactly why the two cases must
    not be merged into a silent ``NULL``.
    """
    if not isinstance(payload, dict):
        return None
    buckets = payload.get("floorValuesArray")
    if not isinstance(buckets, list):
        return None
    ascended_at = _floor_column_index(payload, "floorsAscended", 2)
    total = 0
    seen = False
    for bucket in buckets:
        if not isinstance(bucket, list) or len(bucket) <= ascended_at:
            continue
        ascended = _as_int(bucket[ascended_at])
        if ascended is None or ascended < 0:
            continue
        total += ascended
        seen = True
    return total if seen else None


def extract_intensity_minutes(payload: Any) -> int | None:
    """``get_intensity_minutes_data`` -> the day's intensity minutes, or ``None``.

    Garmin counts a vigorous minute twice, so the daily figure the watch displays is
    ``moderateMinutes + 2 * vigorousMinutes``. That weighting is not assumed from
    documentation: the same payload also carries ``startDayMinutes`` and
    ``endDayMinutes``, the running WEEKLY total before and after the day, and their
    difference matches the weighted sum on every day checked (2026-07-26 .. 08-26,
    including 256 moderate + 215 vigorous = 686 = 756 - 70). ``weeklyTotal`` versus
    ``weeklyModerate`` / ``weeklyVigorous`` closes the same identity at week scale.

    The weekly running totals are deliberately NOT used as the source: they reset on the
    week boundary, so a Monday would read as the whole previous week's residue.
    """
    if not isinstance(payload, dict):
        return None
    moderate = _as_int(payload.get("moderateMinutes"))
    vigorous = _as_int(payload.get("vigorousMinutes"))
    if moderate is None and vigorous is None:
        return None
    return max(moderate or 0, 0) + 2 * max(vigorous or 0, 0)


def extract_training_readiness(payload: Any) -> int | None:
    """``get_training_readiness`` -> the day's score, or ``None``.

    ALWAYS ``None`` on a Forerunner 255: the endpoint answers ``[]`` (verified on all 31
    polled days). Migration 006 anticipates exactly this -- "on Forerunner 255,
    training_readiness [...] can remain empty" -- which is why the column is nullable.
    """
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict):
            score = _as_int(entry.get("score"))
            if score is not None:
                return score
    return None


__all__ = [
    "METRIC_ID_BODY_BATTERY",
    "METRIC_ID_HRV",
    "METRIC_ID_STRESS",
    "SLEEP_STAGE_BY_ACTIVITY_LEVEL",
    "DailyWellnessRow",
    "SleepSessionRow",
    "SleepStageRow",
    "WellnessSampleRow",
    "extract_body_battery",
    "extract_floors",
    "extract_hrv",
    "extract_intensity_minutes",
    "extract_resting_hr",
    "extract_sleep",
    "extract_steps",
    "extract_stress",
    "extract_training_readiness",
    "extract_vo2max",
    "extract_weight_kg",
]
