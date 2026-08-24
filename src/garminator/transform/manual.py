"""Build an :class:`~garminator.transform.activity.ActivityContext` for a MANUAL upload
(migration 014), i.e. a file with no Garmin JSON summary to read sport/timezone/duration
from (cf. ``garminator.transform.summary`` for the Garmin path).

A manual upload carries no ``activityType.typeKey``, no ``startTimeGMT``/``startTimeLocal``
pair, no ``duration``: everything ``build_activity_context`` reads from the Garmin summary
must instead be derived from the archived file itself (FIT ``session`` message, or the TCX
``Activity`` element's ``Sport`` attribute) plus the athlete's ``home_timezone`` fallback,
sanctioned for exactly this case by its own column comment (migrations/003_core_ref.sql).

SAME NON-NEGOTIABLE RULE AS ``summary.py``: an unrecognized sport is NEVER guessed. A FIT
(sport, sub_sport) pair or a TCX ``Sport`` value missing from the tables below raises,
naming the observed value, rather than folding to a default that would silently skew TSS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from garminator.transform.activity import ActivityContext
from garminator.transform.summary import DISCIPLINE_BY_SPORT_CODE

if TYPE_CHECKING:
    from garminator.fit.model import ParsedActivitySource

#: Mapping (FIT ``sport``, FIT ``sub_sport``) string pair -> ``core.sport.sport_code``.
#: Keys with ``sub_sport=None`` are per-``sport`` FALLBACKS, tried when the exact pair is
#: absent -- never a silent default across every ``sport``, cf. module docstring.
SPORT_CODE_BY_FIT_SPORT: dict[tuple[str, str | None], str] = {
    ("running", "treadmill"): "treadmill",
    ("running", "trail"): "trail_running",
    ("running", None): "running",
    ("cycling", "indoor_cycling"): "virtual_cycling",
    ("cycling", "virtual_activity"): "virtual_cycling",
    ("cycling", "spin"): "virtual_cycling",
    ("cycling", None): "cycling",
    ("swimming", "lap_swimming"): "lap_swimming",
    ("swimming", "open_water"): "open_water",
    ("hiking", None): "hiking",
    ("alpine_skiing", None): "resort_skiing",
    ("training", "strength_training"): "strength",
    ("transition", None): "transition",
}

#: TCX ``Activity Sport`` attribute -> ``core.sport.sport_code``. The TCX schema restricts
#: the attribute to exactly "Running" | "Biking" | "Other" (TrainingCenterDatabase v2
#: schema, Sport_t enumeration -- verified against the athlete real TCX, Sport="Biking"): a
#: TCX cannot express indoor vs. outdoor, so a home-trainer ride landing as ``cycling`` is
#: the intended, honest outcome -- never guessed as ``virtual_cycling`` from a format that
#: cannot say so. "Other" is NOT mapped: it is too coarse to trust (cf. module docstring).
SPORT_CODE_BY_TCX_SPORT: dict[str, str] = {
    "Running": "running",
    "Biking": "cycling",
}


class UnknownManualSportError(ValueError):
    """A FIT (sport, sub_sport) pair or TCX Sport value has no entry in the tables above.

    Names the observed value so a human can extend the table knowingly -- see module
    docstring for why this is never guessed around."""


def _sport_code_for_fit(sport: str, sub_sport: str | None) -> str:
    sport_code = SPORT_CODE_BY_FIT_SPORT.get((sport, sub_sport))
    if sport_code is not None:
        return sport_code
    sport_code = SPORT_CODE_BY_FIT_SPORT.get((sport, None))
    if sport_code is not None:
        return sport_code
    msg = (
        f"unknown FIT (sport, sub_sport) = ({sport!r}, {sub_sport!r}) for a manual upload: "
        "no matching sport_code in SPORT_CODE_BY_FIT_SPORT (garminator.transform.manual). "
        "Add the missing row rather than guessing -- wrong classification would silently "
        "skew TSS."
    )
    raise UnknownManualSportError(msg)


def _sport_code_for_tcx(tcx_sport: str | None) -> str:
    if tcx_sport is None:
        msg = (
            "manual TCX upload has no 'Sport' attribute on its root Activity element: "
            "cannot determine sport_code without guessing."
        )
        raise UnknownManualSportError(msg)
    sport_code = SPORT_CODE_BY_TCX_SPORT.get(tcx_sport)
    if sport_code is None:
        msg = (
            f"unknown TCX Sport={tcx_sport!r} for a manual upload: no matching sport_code "
            "in SPORT_CODE_BY_TCX_SPORT (garminator.transform.manual). 'Other' is "
            "deliberately excluded -- too coarse to trust, see module docstring."
        )
        raise UnknownManualSportError(msg)
    return sport_code


def _fit_duration_s(parsed: ParsedActivitySource) -> int:
    if parsed.session is None:
        msg = "manual FIT upload has no 'session' message: cannot derive duration_s."
        raise ValueError(msg)
    total_timer_time = parsed.session.raw.get("total_timer_time")
    if total_timer_time is None:
        msg = "manual FIT upload 'session' message has no 'total_timer_time' field."
        raise ValueError(msg)
    return round(float(total_timer_time))


_MIN_TCX_SAMPLES_FOR_DURATION = 2


def _tcx_duration_s(parsed: ParsedActivitySource) -> int:
    samples = parsed.samples
    if len(samples) < _MIN_TCX_SAMPLES_FOR_DURATION:
        msg = (
            f"manual TCX upload has {len(samples)} sample(s): cannot derive duration_s "
            "from fewer than 2 trackpoints."
        )
        raise ValueError(msg)
    return round(samples[-1].elapsed_s - samples[0].elapsed_s)


def build_manual_activity_context(
    parsed: ParsedActivitySource,
    *,
    activity_id: int,
    home_timezone: str,
    tcx_sport: str | None = None,
) -> ActivityContext:
    """Build an ``ActivityContext`` for a manual upload from the decoded file alone.

    ``home_timezone`` is ``core.athlete.home_timezone`` (migrations/003_core_ref.sql):
    its own column comment names this exact case ("fallback value when an activity's
    timezone is indeterminable") as its sanctioned use -- a manual upload carries no
    ``startTimeGMT``/``startTimeLocal`` pair to derive a real offset from.

    ``tcx_sport`` is ignored for a FIT source (sport comes from ``parsed.session.raw``
    instead) and REQUIRED (via :func:`_sport_code_for_tcx`, which raises on ``None``)
    for a TCX source -- the caller passes ``parsed.tcx_sport`` there.
    """
    if parsed.start_time is None:
        msg = (
            f"manual upload (activity_id={activity_id}): decoded file has no usable "
            "start_time -- cannot compute tz_offset_s / start_time_local without one."
        )
        raise ValueError(msg)

    if parsed.source_format == "fit":
        if parsed.session is None:
            msg = f"manual FIT upload (activity_id={activity_id}) has no 'session' message."
            raise ValueError(msg)
        sport = parsed.session.raw.get("sport")
        if sport is None:
            msg = f"manual FIT upload (activity_id={activity_id}): 'session.sport' missing."
            raise ValueError(msg)
        sub_sport = parsed.session.raw.get("sub_sport")
        sport_code = _sport_code_for_fit(str(sport), None if sub_sport is None else str(sub_sport))
        duration_s = _fit_duration_s(parsed)
    elif parsed.source_format == "tcx":
        sport_code = _sport_code_for_tcx(tcx_sport)
        duration_s = _tcx_duration_s(parsed)
    else:
        msg = (
            f"manual upload (activity_id={activity_id}): unknown source_format "
            f"{parsed.source_format!r} (neither 'fit' nor 'tcx')."
        )
        raise ValueError(msg)

    if duration_s <= 0:
        msg = f"manual upload (activity_id={activity_id}): derived duration_s={duration_s} <= 0."
        raise ValueError(msg)

    discipline = DISCIPLINE_BY_SPORT_CODE[sport_code]

    # parsed.start_time is UTC-aware (cf. ParsedActivitySource docstring); localize with
    # the athlete's home timezone rather than a hardcoded offset, so DST is handled per
    # the real calendar date instead of a fixed +1h/+2h assumption.
    start_time_utc = parsed.start_time
    if start_time_utc.tzinfo is None:
        msg = (
            f"manual upload (activity_id={activity_id}): decoded start_time is naive, "
            "expected UTC-aware."
        )
        raise ValueError(msg)
    tz = ZoneInfo(home_timezone)
    start_time_local_aware = start_time_utc.astimezone(tz)
    tz_offset_s = int(start_time_local_aware.utcoffset().total_seconds())  # type: ignore[union-attr]
    start_time_local = start_time_local_aware.replace(tzinfo=None)

    return ActivityContext(
        activity_id=activity_id,
        sport_code=sport_code,
        discipline=discipline,
        duration_s=duration_s,
        start_time_local=start_time_local,
        tz_offset_s=tz_offset_s,
        garmin_type_key=None,
        summary_sha256=None,
        is_parent=False,
        source="manual",
    )


__all__ = [
    "SPORT_CODE_BY_FIT_SPORT",
    "SPORT_CODE_BY_TCX_SPORT",
    "UnknownManualSportError",
    "build_manual_activity_context",
]
