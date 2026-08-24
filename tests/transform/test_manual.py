"""Tests for :mod:`garminator.transform.manual` -- building an ``ActivityContext`` for a
manual upload (migration 014), with no Garmin JSON summary to read from.

All fixtures are synthetic (see ``tests/transform/conftest.py`` module docstring): no
byte of the athlete's real ``running.fit``/``swim.fit``/``bike.tcx`` is used.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from garminator.fit.model import ParsedActivitySource, SessionSummary
from garminator.transform.manual import (
    UnknownManualSportError,
    build_manual_activity_context,
)

from .conftest import make_sample

ACTIVITY_ID = 9000000000000000001


def _fit_parsed(
    *, sport: str, sub_sport: str | None, total_timer_time: float, start_time: datetime | None
) -> ParsedActivitySource:
    raw: dict[str, object] = {"sport": sport, "total_timer_time": total_timer_time}
    if sub_sport is not None:
        raw["sub_sport"] = sub_sport
    return ParsedActivitySource(
        source_format="fit",
        samples=[],
        laps=[],
        swim_lengths=[],
        session=SessionSummary(raw=raw),
        start_time=start_time,
    )


def _tcx_parsed(
    *, tcx_sport: str | None, sample_count: int, start_time: datetime | None
) -> ParsedActivitySource:
    samples = [make_sample(i * 60) for i in range(sample_count)]
    return ParsedActivitySource(
        source_format="tcx",
        samples=samples,
        laps=[],
        swim_lengths=[],
        session=None,
        start_time=start_time,
        tcx_sport=tcx_sport,
    )


# -- FIT (sport, sub_sport) -> sport_code -----------------------------------------------


@pytest.mark.parametrize(
    ("sport", "sub_sport", "expected"),
    [
        ("running", "treadmill", "treadmill"),
        ("running", "trail", "trail_running"),
        ("running", None, "running"),
        ("running", "some_unmapped_sub_sport", "running"),  # falls back to sport-only entry
        ("cycling", "indoor_cycling", "virtual_cycling"),
        ("cycling", "virtual_activity", "virtual_cycling"),
        ("cycling", "spin", "virtual_cycling"),
        ("cycling", None, "cycling"),
        ("swimming", "lap_swimming", "lap_swimming"),
        ("swimming", "open_water", "open_water"),
        ("hiking", None, "hiking"),
        ("hiking", "anything", "hiking"),
        ("alpine_skiing", None, "resort_skiing"),
        ("training", "strength_training", "strength"),
        ("transition", None, "transition"),
    ],
)
def test_fit_sport_code_mapping(sport: str, sub_sport: str | None, expected: str) -> None:
    parsed = _fit_parsed(
        sport=sport,
        sub_sport=sub_sport,
        total_timer_time=3600.0,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris"
    )
    assert context.sport_code == expected


def test_fit_unknown_sport_raises_naming_the_value() -> None:
    parsed = _fit_parsed(
        sport="curling",
        sub_sport=None,
        total_timer_time=3600.0,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    with pytest.raises(UnknownManualSportError, match="curling"):
        build_manual_activity_context(parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris")


# -- TCX Sport attribute -> sport_code ---------------------------------------------------


@pytest.mark.parametrize(
    ("tcx_sport", "expected"),
    [
        ("Running", "running"),
        ("Biking", "cycling"),
    ],
)
def test_tcx_sport_code_mapping(tcx_sport: str, expected: str) -> None:
    parsed = _tcx_parsed(
        tcx_sport=tcx_sport,
        sample_count=3,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport=tcx_sport
    )
    assert context.sport_code == expected


def test_tcx_other_sport_raises() -> None:
    """ "Other" is too coarse to trust -- a home-trainer ride would be misclassified as
    an outdoor discipline just as easily as the reverse."""
    parsed = _tcx_parsed(
        tcx_sport="Other", sample_count=3, start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)
    )
    with pytest.raises(UnknownManualSportError, match="Other"):
        build_manual_activity_context(
            parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport="Other"
        )


def test_tcx_missing_sport_raises() -> None:
    parsed = _tcx_parsed(
        tcx_sport=None, sample_count=3, start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)
    )
    with pytest.raises(UnknownManualSportError):
        build_manual_activity_context(
            parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport=None
        )


# -- duration derivation -------------------------------------------------------------


def test_fit_duration_rounded_from_total_timer_time() -> None:
    parsed = _fit_parsed(
        sport="running",
        sub_sport=None,
        total_timer_time=4642.844,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris"
    )
    assert context.duration_s == 4643


def test_fit_non_positive_duration_raises() -> None:
    parsed = _fit_parsed(
        sport="running",
        sub_sport=None,
        total_timer_time=0.0,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="duration_s"):
        build_manual_activity_context(parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris")


def test_tcx_duration_from_last_minus_first_sample() -> None:
    parsed = _tcx_parsed(
        tcx_sport="Biking",
        sample_count=61,  # elapsed_s 0, 60, ..., 3600
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport="Biking"
    )
    assert context.duration_s == 3600


def test_tcx_non_positive_duration_raises() -> None:
    """Fewer than 2 usable trackpoints: no time span can be derived."""
    parsed = _tcx_parsed(
        tcx_sport="Biking", sample_count=1, start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="duration_s"):
        build_manual_activity_context(
            parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport="Biking"
        )


# -- tz_offset_s / start_time_local: catches a naive hardcoded offset -----------------


def test_tz_offset_summer_is_plus_two_hours_in_paris() -> None:
    """CEST (summer): UTC+2."""
    parsed = _fit_parsed(
        sport="running",
        sub_sport=None,
        total_timer_time=3600.0,
        start_time=datetime(2026, 7, 15, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris"
    )
    assert context.tz_offset_s == 7200
    assert context.start_time_local == datetime(2026, 7, 15, 9, 0, 0)
    assert context.start_time_local.tzinfo is None


def test_tz_offset_winter_is_plus_one_hour_in_paris() -> None:
    """CET (winter): UTC+1."""
    parsed = _fit_parsed(
        sport="running",
        sub_sport=None,
        total_timer_time=3600.0,
        start_time=datetime(2026, 1, 15, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris"
    )
    assert context.tz_offset_s == 3600
    assert context.start_time_local == datetime(2026, 1, 15, 8, 0, 0)


def test_missing_start_time_raises() -> None:
    parsed = _tcx_parsed(tcx_sport="Biking", sample_count=0, start_time=None)
    with pytest.raises(ValueError, match="start_time"):
        build_manual_activity_context(
            parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris", tcx_sport="Biking"
        )


# -- other context fields -------------------------------------------------------------


def test_source_and_discipline_and_static_fields() -> None:
    parsed = _fit_parsed(
        sport="cycling",
        sub_sport="indoor_cycling",
        total_timer_time=1800.0,
        start_time=datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC),
    )
    context = build_manual_activity_context(
        parsed, activity_id=ACTIVITY_ID, home_timezone="Europe/Paris"
    )
    assert context.source == "manual"
    assert context.discipline == "bike"
    assert context.garmin_type_key is None
    assert context.summary_sha256 is None
    assert context.is_parent is False
    assert context.activity_id == ACTIVITY_ID
