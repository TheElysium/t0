"""100% synthetic fixtures for tests of the ``t0.transform`` layer.

NO actual activity byte (``running.fit``/``swim.fit``/``bike.tcx``/
``Activities.csv``/``metrics&wellness_1year/``) appears here: these files contain GPS
traces that identify the athlete's home (cf. layer mandate). All values below are
constructed by hand, chosen to be round and easy to verify mentally (200 W for 60s has
an average of 200 W, not a figure extracted from a real file)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from t0.fit.model import (
    LapRow,
    ParsedActivitySource,
    SampleRow,
    SessionSummary,
    SwimLengthRow,
)
from t0.load.thresholds import Threshold
from t0.transform.activity import ActivityContext

ACTIVITY_ID = 1
ATHLETE_ID = 7
START = datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)

#: Synthetic thresholds covering three disciplines, in the style of
#: tests/load/test_activity_load.py.
BIKE_THRESHOLD = Threshold(
    discipline="bike", valid_from=date(2026, 1, 1), valid_to=None, threshold_id=1, ftp_watts=220.0
)
RUN_THRESHOLD = Threshold(
    discipline="run",
    valid_from=date(2026, 1, 1),
    valid_to=None,
    threshold_id=2,
    rftp_watts=None,  # never guessed for this athlete: cf. layer mandate.
    ftp_pace_s_per_km=270.0,  # 4:30/km
)
SWIM_THRESHOLD = Threshold(
    discipline="swim", valid_from=date(2026, 1, 1), valid_to=None, threshold_id=3, css_m_s=0.909091
)
ALL_THRESHOLD = Threshold(
    discipline="all",
    valid_from=date(2026, 1, 1),
    valid_to=None,
    threshold_id=4,
    lthr_bpm=170.0,
    hr_max_bpm=190.0,
    hr_rest_bpm=55.0,
)


def make_sample(
    elapsed_s: int,
    *,
    power: int | None = None,
    heart_rate: int | None = None,
    speed_mps: float | None = None,
    distance_m: float | None = None,
    altitude_m: float | None = None,
    cadence: int | None = None,
) -> SampleRow:
    """A synthetic ``SampleRow`` at ``elapsed_s`` seconds from start."""
    return SampleRow(
        activity_id=ACTIVITY_ID,
        t=START + timedelta(seconds=elapsed_s),
        elapsed_s=elapsed_s,
        power=power,
        heart_rate=heart_rate,
        speed_mps=speed_mps,
        distance_m=distance_m,
        altitude_m=altitude_m,
        cadence=cadence,
    )


def bike_context(*, duration_s: int = 3600, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="virtual_cycling",  # home-trainer TCX, cf. athlete profile.
        discipline="bike",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def run_context(*, duration_s: int = 3600, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="running",
        discipline="run",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def swim_context(*, duration_s: int = 1800, activity_id: int = ACTIVITY_ID) -> ActivityContext:
    return ActivityContext(
        activity_id=activity_id,
        sport_code="lap_swimming",
        discipline="swim",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
    )


def multisport_context(
    *, duration_s: int = 8000, activity_id: int = ACTIVITY_ID
) -> ActivityContext:
    """Multisport event parent (2026-09-05 triathlon shape): is_parent, no Garmin
    type_key beyond the summary's -- cf. build_activity_context."""
    return ActivityContext(
        activity_id=activity_id,
        sport_code="multisport",
        discipline="other",
        duration_s=duration_s,
        start_time_local=START,
        tz_offset_s=7200,
        garmin_type_key="multi_sport",
        is_parent=True,
    )


def make_session(
    start: datetime,
    *,
    sport: str,
    sub_sport: str | None = None,
    duration_s: float,
    distance_m: float | None = None,
) -> SessionSummary:
    """Hand-built FIT ``session`` message -- only the fields the split reads.

    ``start_time`` is the decoded datetime the reader would carry (cf.
    ``t0.fit.reader``: taken as-is from the session message)."""
    raw: dict[str, Any] = {
        "start_time": start,
        "total_timer_time": duration_s,
        "sport": sport,
    }
    if sub_sport is not None:
        raw["sub_sport"] = sub_sport
    if distance_m is not None:
        raw["total_distance"] = distance_m
    return SessionSummary(raw=raw)


def parsed_source(
    samples: list[SampleRow],
    *,
    laps: list[LapRow] | None = None,
    swim_lengths: list[SwimLengthRow] | None = None,
    session: SessionSummary | None = None,
    sessions: tuple[SessionSummary, ...] | None = None,
) -> ParsedActivitySource:
    """``sessions`` overrides the per-file session list when given; by default it is
    derived (``session`` alone, or ``(session,)`` when supplied): a single-sport file,
    the shape every pre-multisport test exercises."""
    if sessions is None:
        sessions = () if session is None else (session,)
    return ParsedActivitySource(
        source_format="fit",
        samples=samples,
        laps=laps or [],
        swim_lengths=swim_lengths or [],
        session=sessions[0] if sessions else None,
        start_time=samples[0].t if samples else START,
        sessions=sessions,
    )
