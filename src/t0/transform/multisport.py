"""Split of a multisport FIT file into legs -- one :class:`CoreActivityBundle` per session.

VERIFIED reality that motivates this module (2026-09-05 triathlon,
activity 24243770345): ``get_activities_by_date`` NEVER publishes the legs of a
multisport event -- the list carries only the parent (``isParent: true``, no children
field), identically for 24 h. The single-device FIT file (five sessions: swim, T1,
bike, T2, run) is the ONLY place the legs exist in data. The transform therefore
splits the sessions into leg bundles; the schema was already shaped for them
(``core.activity.parent_activity_id`` / ``leg_order``, migration 004, index
``activity_parent_idx``) and every load view already counts them (``is_parent = false``)
while excluding the parent -- so the marts need NO change.

The split reuses the whole existing pipeline per leg: each leg goes through
``build_core_rows`` with a per-session ``ParsedActivitySource`` slice and its own
``ActivityContext``, hence per-leg NP/GAP/HR histogram/power curve and per-discipline
TSS thresholds (bike: bike_power then hr_hrtss, open_water: swim_css then hr_hrtss,
transition: hr_hrtss only). The parent keeps its bundle unchanged (whole-file samples,
canonical hr_hrtss as before): keeping its metrics is deliberate -- see migration 023.

SAME NON-NEGOTIABLE RULE AS ``summary.py``/``manual.py``: an unrecognized (sport,
sub_sport) pair raises, naming the observed value. A leg silently folded to a wrong
sport would skew per-discipline TSS AND be invisible (no parse_error), the worst
combination -- the file goes to ``raw.fit_file.parse_error`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from t0.fit.model import ParsedActivitySource
from t0.transform.activity import ActivityContext, build_core_rows
from t0.transform.manual import sport_code_for_fit
from t0.transform.summary import DISCIPLINE_BY_SPORT_CODE

if TYPE_CHECKING:
    from t0.fit.model import LapRow, SampleRow, SwimLengthRow
    from t0.load.thresholds import Threshold
    from t0.load.trimp import Sex
    from t0.transform.rows import CoreActivityBundle

#: A multisport file, by FIT shape: more than one ``session`` message. A single-sport
#: file (the common case) has exactly one and is never split -- the transform output
#: is byte-identical to pre-split behavior for it.
MIN_SESSIONS_FOR_SPLIT = 2


@dataclass(frozen=True, slots=True)
class _SessionWindow:
    """One session's slice of the decoded file, in absolute time.

    ``duration_s`` is the ALREADY-VALIDATED (> 0) timer time, carried here so the
    split loop never re-reads an optional field it may only trust once.
    """

    start: datetime
    duration_s: int
    #: ``None`` for the LAST session: its samples are never cut at the right edge --
    #: a sample past the last session's timer end (cool-down still recording) is data,
    #: never silently dropped.
    end: datetime | None


@dataclass(frozen=True, slots=True)
class LegPlan:
    """Everything :func:`build_leg_bundle` needs for one leg, BEFORE an activity_id.

    The identifier is resolved by the caller (idempotently, from
    ``core.leg_activity_id_seq`` / existing rows -- see ``t0.transform.store``):
    the split itself is pure and must not touch the database.
    """

    leg_order: int
    sport_code: str
    discipline: str
    #: Seconds from the FILE start to this session's start. Re-bases sample
    #: ``elapsed_s`` (a leg starts at 0, not at 2 239 s into the event) and anchors
    #: ``start_time_local`` on the parent's, without inventing a timezone.
    offset_s: int
    start_time: datetime
    duration_s: int
    elapsed_s: int | None
    moving_s: int | None
    distance_m: float | None
    ascent_m: float | None
    descent_m: float | None
    samples: tuple[SampleRow, ...]
    laps: tuple[LapRow, ...]
    swim_lengths: tuple[SwimLengthRow, ...]


def _session_float(session_raw: dict[str, Any], field: str) -> float | None:
    value = session_raw.get(field)
    return None if value is None else float(value)


def _session_int(session_raw: dict[str, Any], field: str) -> int | None:
    value = session_raw.get(field)
    return None if value is None else int(round(float(value)))


def _windows(
    decoded: ParsedActivitySource,
) -> tuple[tuple[dict[str, Any], _SessionWindow], ...]:
    """Sessions ordered by start, each paired with its half-open sample window.

    Missing ``start_time`` / ``total_timer_time`` raise rather than default: a leg
    without bounds cannot be cut from the sample stream, and guessing the boundary
    would misattribute whole minutes of one sport's load to another.
    """
    starts: list[tuple[dict[str, Any], datetime, float]] = []
    for session in decoded.sessions:
        raw = session.raw
        start = raw.get("start_time")
        if not isinstance(start, datetime):
            msg = (
                "multisport split: FIT session message without usable 'start_time' "
                f"(fields present: {sorted(raw.keys())!r})."
            )
            raise ValueError(msg)
        duration = _session_float(raw, "total_timer_time")
        if duration is None:
            msg = "multisport split: FIT session message without 'total_timer_time'."
            raise ValueError(msg)
        if duration <= 0:
            msg = f"multisport split: session total_timer_time={duration!r} <= 0."
            raise ValueError(msg)
        starts.append((raw, start, duration))
    starts.sort(key=lambda triple: triple[1])
    return tuple(
        (
            raw,
            _SessionWindow(
                start=start,
                duration_s=round(duration),
                end=None if index == len(starts) - 1 else starts[index + 1][1],
            ),
        )
        for index, (raw, start, duration) in enumerate(starts)
    )


def _in_window(t: datetime, window: _SessionWindow) -> bool:
    return t >= window.start and (window.end is None or t < window.end)


def split_multisport_legs(
    decoded: ParsedActivitySource, parent_context: ActivityContext
) -> tuple[LegPlan, ...]:
    """Slice a multisport file into one :class:`LegPlan` per FIT session.

    ``parent_context`` only anchors wall-clock time (``start_time_local``,
    ``tz_offset_s``); it is never re-derived here. Samples BEFORE the first session's
    start cannot be attributed to any leg and raise rather than being dropped: the
    sample stream is the athlete's effort, losing a second of it silently is exactly
    the defect this layer exists to prevent.
    """
    if len(decoded.sessions) < MIN_SESSIONS_FOR_SPLIT:
        return ()
    if decoded.start_time is None:
        msg = "multisport split: decoded file has no start_time to anchor sessions."
        raise ValueError(msg)
    file_start = decoded.start_time
    windows = _windows(decoded)
    unassigned = [s for s in decoded.samples if s.t < windows[0][1].start]
    if unassigned:
        msg = (
            f"multisport split: {len(unassigned)} sample(s) before the first session start "
            f"({windows[0][1].start.isoformat()}) -- no leg to attribute them to."
        )
        raise ValueError(msg)
    plans: list[LegPlan] = []
    # FIT 'distance' is CUMULATIVE over the whole file (VERIFIED: the 2026-09-05 run
    # leg's last sample carries ~50 km, the event's total). Without re-basing, every
    # leg after the first would inherit the event's running total as its own distance.
    # Baseline = the cumulative distance reached just BEFORE the session starts, from
    # the last sample that carries a distance -- the swim leg (file head) has none.
    distance_baseline = 0.0
    for order, (raw, window) in enumerate(windows, start=1):
        samples = tuple(
            replace(
                sample,
                elapsed_s=round((sample.t - window.start).total_seconds()),
                distance_m=(
                    None
                    if sample.distance_m is None
                    else sample.distance_m - distance_baseline
                ),
            )
            for sample in decoded.samples
            if _in_window(sample.t, window)
        )
        later_distances = [
            sample.distance_m
            for sample in decoded.samples
            if sample.distance_m is not None and _in_window(sample.t, window)
        ]
        if later_distances:
            # Cumulative is monotonic: the max over THIS window is its last value,
            # i.e. the baseline the NEXT leg starts from.
            distance_baseline = max(later_distances)
        laps = tuple(lap for lap in decoded.laps if _in_window(lap.start_time, window))
        lengths = tuple(
            length for length in decoded.swim_lengths if _in_window(length.start_time, window)
        )
        sport = raw.get("sport")
        if sport is None:
            msg = "multisport split: session without 'sport' field."
            raise ValueError(msg)
        sub_sport = raw.get("sub_sport")
        sport_code = sport_code_for_fit(str(sport), None if sub_sport is None else str(sub_sport))
        offset_s = round((window.start - file_start).total_seconds())
        plans.append(
            LegPlan(
                leg_order=order,
                sport_code=sport_code,
                discipline=DISCIPLINE_BY_SPORT_CODE[sport_code],
                offset_s=offset_s,
                start_time=window.start,
                duration_s=window.duration_s,
                elapsed_s=_session_int(raw, "total_elapsed_time"),
                moving_s=_session_int(raw, "total_moving_time"),
                distance_m=_session_float(raw, "total_distance"),
                ascent_m=_session_float(raw, "total_ascent"),
                descent_m=_session_float(raw, "total_descent"),
                samples=samples,
                laps=laps,
                swim_lengths=lengths,
            )
        )
    return tuple(plans)


def build_leg_bundle(
    plan: LegPlan,
    *,
    leg_activity_id: int,
    parent_context: ActivityContext,
    athlete_id: int,
    fit_file_id: int | None,
    threshold: Threshold | None,
    all_threshold: Threshold | None,
    sex: Sex,
) -> CoreActivityBundle:
    """One leg through the standard pipeline: sliced source + leg context ->
    ``build_core_rows`` -- no leg-specific computation anywhere.

    Samples are re-tagged with the leg identifier (``build_core_rows`` refuses a
    source/context mismatch, rightly: assembling one activity's summary with another's
    file). The leg ``ParsedActivitySource`` keeps ``start_time`` = session start, so
    ``core.activity.start_time`` is the leg's OWN departure instant, not the event's.
    """
    decoded = ParsedActivitySource(
        source_format="fit",
        samples=[replace(s, activity_id=leg_activity_id) for s in plan.samples],
        laps=[replace(lap, activity_id=leg_activity_id) for lap in plan.laps],
        swim_lengths=[replace(length, activity_id=leg_activity_id) for length in plan.swim_lengths],
        session=None,
        start_time=plan.start_time,
    )
    context = ActivityContext(
        activity_id=leg_activity_id,
        sport_code=plan.sport_code,
        discipline=plan.discipline,
        duration_s=plan.duration_s,
        start_time_local=parent_context.start_time_local + timedelta(seconds=plan.offset_s),
        tz_offset_s=parent_context.tz_offset_s,
        garmin_type_key=None,
        elapsed_s=plan.elapsed_s,
        moving_s=plan.moving_s,
        distance_m=plan.distance_m,
        ascent_m=plan.ascent_m,
        descent_m=plan.descent_m,
        parent_activity_id=parent_context.activity_id,
        leg_order=plan.leg_order,
        primary_device_id=parent_context.primary_device_id,
        source="leg",
    )
    return build_core_rows(
        decoded,
        context,
        athlete_id=athlete_id,
        fit_file_id=fit_file_id,
        threshold=threshold,
        all_threshold=all_threshold,
        sex=sex,
    )


__all__ = [
    "LegPlan",
    "build_leg_bundle",
    "split_multisport_legs",
]
