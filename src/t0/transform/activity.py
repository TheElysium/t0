"""``build_core_rows`` : pure chain of decoded + thresholds -> ``CoreActivityBundle``.

Single orchestration function for this layer. It performs no "new" computation: it calls,
in the correct order with the right series, already-pure and -tested functions from
``t0.compute`` (L4) and ``t0.load`` (L5). Nothing here accesses disk,
network, or PostgreSQL.

ASSUMED DEVIATION FROM PROPOSED SIGNATURE IN SPEC
---------------------------------------------------
The spec envisioned ``build_core_rows(decoded, *, athlete_id, fit_file_id,
threshold, all_threshold, sex)``. That is not sufficient: ``ParsedActivitySource``
(``t0.fit.model``) carries NEITHER sport NOR elapsed time NOR local time of an
activity. For a FIT file, this information exists somewhere in the file (``session``
message), but ``decode_fit`` deliberately leaves it in ``SessionSummary.raw`` -- an
untyped dict -- because its own docstring explains it: only 6 comparison fields are
typed; the rest is "out of scope for L3". For a TCX, there is no ``session`` message at
all: ``ParsedActivitySource.session`` is ``None`` (VERIFIED, cf. ``tcx/reader.py`` --
addendum §2.2), and that is precisely the format of ALL this athlete's indoor trainer
sessions.

Rather than guessing this information from the samples (guessing a sport or elapsed time
from a series of points is exactly the kind of invention this project forbids), this
layer adds an explicit parameter ``context: ActivityContext`` carrying what only the
caller (the JSON summary ``get_activity``, L2/L6, outside this layer's scope) can know
reliably. Everything that IS computable from the samples (averages, NP, GAP, total
distance...) is here and NOT requested from ``ActivityContext`` -- the gap is as small
as possible.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from t0.compute import (
    average,
    average_nonzero,
    decoupling_pct,
    efficiency_factor,
    normalized_graded_pace_speed_mps,
    normalized_power,
    power_duration_curve,
    regularize_series,
    smooth_grade_series,
)
from t0.compute import (
    hr_histogram as compute_hr_histogram,
)
from t0.compute import (
    np_reliable as compute_np_reliable,
)
from t0.load.activity_load import (
    ActivityLoadInput,
    ActivityMetricRow,
    build_activity_metric_rows,
    needs_manual_rpe,
)
from t0.transform.rows import (
    ActivityHrHistogramRow,
    ActivityPowerCurveRow,
    ActivityRow,
    CoreActivityBundle,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from t0.compute import RegularizedSeries
    from t0.fit.model import ParsedActivitySource, SampleRow, SwimLengthRow
    from t0.load.thresholds import Threshold
    from t0.load.trimp import Sex

#: core.activity_power_curve kind carried by power, by discipline.
_POWER_CURVE_KIND = {"bike": "bike_power", "run": "run_power"}

#: minimum speed+distance samples to attempt smoothed grade calculation
#: (``smooth_grade_series`` needs at least two points for a window).
_MIN_SAMPLES_FOR_GRADE = 2

#: method_code reused for efficiency_factor / decoupling_pct, by discipline
#: (cf. ``_load_extra_metric_rows`` docstring for justification).
_DECOUPLING_METHOD_CODE = {"bike": "bike_power", "run": "run_gap"}


@dataclass(frozen=True, slots=True)
class ActivityContext:
    """What ``ParsedActivitySource`` cannot tell us about an activity (see module
    docstring). Supplied by the caller, never guessed.

    ``distance_m`` / ``ascent_m`` / ``descent_m`` are fallbacks used ONLY when no
    sample carries the corresponding information (as in pool swimming, where
    ``core.sample`` is empty by design -- see ``migrations/005_core_series.sql``,
    ``core.swim_length`` comment): when usable samples exist, ``build_core_rows``
    always trusts them first.
    """

    activity_id: int
    sport_code: str
    discipline: str  # 'bike' | 'run' | 'swim' | 'other'
    duration_s: int
    start_time_local: datetime
    tz_offset_s: int
    garmin_type_key: str | None = None
    elapsed_s: int | None = None
    moving_s: int | None = None
    distance_m: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None
    calories: int | None = None
    name: str | None = None
    parent_activity_id: int | None = None
    is_parent: bool = False
    leg_order: int | None = None
    primary_device_id: int | None = None
    summary_sha256: bytes | None = None
    source: str = "garmin"
    """``core.activity.source`` (migration 014). Defaulted to ``'garmin'`` so every
    existing caller (``build_activity_context``, Garmin JSON summary) keeps producing
    a valid context unchanged; ``build_manual_activity_context`` sets it to ``'manual'``."""


def _channel(
    samples: list[SampleRow], getter: Callable[[SampleRow], float | int | None]
) -> tuple[list[int], list[float]]:
    """Sub-series ``(elapsed_s, values)`` restricted to samples where ``getter``
    returns a usable value -- a brief absence (sensor disconnection) is NEVER confused
    with an actual value of 0."""
    elapsed: list[int] = []
    values: list[float] = []
    for sample in samples:
        value = getter(sample)
        if value is not None:
            elapsed.append(sample.elapsed_s)
            values.append(float(value))
    return elapsed, values


def _build_power_curve_rows(
    activity_id: int, kind: str, values: tuple[float, ...]
) -> tuple[ActivityPowerCurveRow, ...]:
    if not values:
        return ()
    curve = power_duration_curve(values)
    return tuple(
        ActivityPowerCurveRow(activity_id=activity_id, kind=kind, duration_s=duration, value=value)
        for duration, value in sorted(curve.items())
        if value is not None
    )


def _run_gap_series(
    samples: list[SampleRow],
) -> tuple[list[float], list[float | None]]:
    """Speed (m/s) and smoothed grade, aligned, restricted to samples carrying both
    speed AND cumulative distance (``smooth_grade_series`` needs a gapless distance
    series for windowing -- §3.3.2 of the spec cited in ``minetti.py``). Altitude
    tolerates isolated ``None`` values."""
    usable = [s for s in samples if s.speed_mps is not None and s.distance_m is not None]
    if len(usable) < _MIN_SAMPLES_FOR_GRADE:
        return [], []
    speed = [float(s.speed_mps) for s in usable]  # type: ignore[arg-type]  # filtered above
    distance = [float(s.distance_m) for s in usable]  # type: ignore[arg-type]
    altitude = [s.altitude_m for s in usable]
    grade = smooth_grade_series(distance, altitude)
    return speed, grade


def _load_extra_metric_rows(
    *,
    activity_id: int,
    discipline: str,
    output_full: float | None,
    output_half1: float | None,
    output_half2: float | None,
    avg_hr_full: float | None,
    avg_hr_half1: float | None,
    avg_hr_half2: float | None,
) -> tuple[ActivityMetricRow, ...]:
    """Rows for ``efficiency_factor`` (full activity) and ``decoupling_pct`` (two
    halves of equal duration), emitted directly here rather than by
    :func:`t0.load.activity_load.build_activity_metric_rows`.

    JUSTIFIED CHOICE: ``ActivityLoadInput`` (L5 contract) receives only scalars
    already aggregated over the full activity -- that is deliberate, see its docstring.
    Decoupling NEEDS the two halves of the series, data that L5 structurally does not
    and should not know (it would remain pure if added, but its contract would then mix
    two levels of granularity). Computing the two halves is the natural work of THIS
    module, which already has samples in hand for NP/GAP -- adding ``efficiency_factor``/
    ``decoupling_pct`` here avoids a round-trip of already-computed data.

    ``method_code``: no dedicated code exists in ``core.load_method`` (only 7 codes
    seeded by migrations/003; no migrations permitted in this layer). Conservative,
    documented choice: reuse the method_code of the quantity that SERVES as "output" to
    the computation (``bike_power`` for bike power, ``run_gap`` for grade-adjusted run
    pace -- never ``run_power``, because rFTP is NULL by explicit athlete decision and
    GAP is the only method actually available for running, see layer vigilance points).
    Only bike and run are covered: no decoupling formula is specified for swimming in
    this layer, and inventing an output measure for it would not be justified -- explicit
    out-of-scope, flagged here.
    """
    if discipline not in _DECOUPLING_METHOD_CODE:
        return ()
    method_code = _DECOUPLING_METHOD_CODE[discipline]

    ef_full = efficiency_factor(output_full, avg_hr_full)
    ef1 = efficiency_factor(output_half1, avg_hr_half1)
    ef2 = efficiency_factor(output_half2, avg_hr_half2)
    decoupling = decoupling_pct(ef1, ef2)

    return (
        ActivityMetricRow(
            activity_id=activity_id,
            metric="efficiency_factor",
            method_code=method_code,
            value=ef_full,
            is_canonical=ef_full is not None,
            threshold_id=None,  # no threshold enters this computation (raw output / HR).
        ),
        ActivityMetricRow(
            activity_id=activity_id,
            metric="decoupling_pct",
            method_code=method_code,
            value=decoupling,
            is_canonical=decoupling is not None,
            threshold_id=None,
        ),
    )


@dataclass(frozen=True, slots=True)
class _PowerChannel:
    """Intermediate result of the power channel -- factored out of :func:`build_core_rows`
    to stay under this module's complexity threshold (PLR0915), without changing the
    computation itself."""

    elapsed: list[int]
    values: list[float]
    regularized: RegularizedSeries
    np_watts: float | None
    np_reliable: bool | None
    avg_power: float | None
    avg_power_nonzero: float | None
    max_power: float | None


def _power_channel(samples: list[SampleRow]) -> _PowerChannel:
    # -- Power channel: only regularized channel in this layer (cf. comment on
    # core.activity.imputed_seconds, explicitly about power).
    elapsed, values = _channel(samples, lambda s: s.power)
    reg = regularize_series(elapsed, values, fill_value=0.0)
    np_watts = normalized_power(reg.values)
    return _PowerChannel(
        elapsed=elapsed,
        values=values,
        regularized=reg,
        np_watts=np_watts,
        np_reliable=compute_np_reliable(reg.total_seconds) if np_watts is not None else None,
        avg_power=average(values) if values else None,
        avg_power_nonzero=average_nonzero(values) if values else None,
        max_power=max(values) if values else None,
    )


@dataclass(frozen=True, slots=True)
class _HrChannel:
    """Intermediate result of the heart-rate channel (same reason as
    :class:`_PowerChannel`)."""

    elapsed: list[int]
    values: list[float]
    avg_hr_raw: float | None  # unrounded, reused as-is by EF/decoupling.
    avg_hr: int | None
    max_hr: int | None


def _hr_channel(samples: list[SampleRow]) -> _HrChannel:
    elapsed, values = _channel(samples, lambda s: s.heart_rate)
    avg_hr_raw = average(values) if values else None
    return _HrChannel(
        elapsed=elapsed,
        values=values,
        avg_hr_raw=avg_hr_raw,
        avg_hr=round(avg_hr_raw) if avg_hr_raw is not None else None,
        max_hr=round(max(values)) if values else None,
    )


@dataclass(frozen=True, slots=True)
class _RunGap:
    """Intermediate result of GAP/NGP running (same reason as :class:`_PowerChannel`)."""

    ngp_speed_mps: float | None
    gap_applied: bool
    speed: list[float]
    grade: list[float | None]


def _run_gap(discipline: str, samples: list[SampleRow]) -> _RunGap:
    # -- GAP / NGP: run only (§3.3.4 of spec, cf. minetti.py).
    if discipline != "run":
        return _RunGap(None, False, [], [])
    speed, grade = _run_gap_series(samples)
    if not speed:
        return _RunGap(None, False, speed, grade)
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    # "Applied" means usable elevation actually affected the result -- not just that a
    # calculation happened (entirely-None grade falls back to f(0)=1 everywhere, cf.
    # minetti.py).
    gap_applied = ngp is not None and any(g is not None for g in grade)
    return _RunGap(ngp, gap_applied, speed, grade)


def _swim_time(
    context: ActivityContext, swim_lengths: list[SwimLengthRow]
) -> tuple[float | None, str | None]:
    # -- Swimming: active swim time reconstructed from FIT lengths (§4.3).
    # A swim WITHOUT lengths (open water) has no usable second-by-second data -- this is
    # NOT an error (empty core.sample is a legitimate state, cf. migrations/004,
    # core.swim_length comment): we fall back to total elapsed time, less reliable,
    # hence the "fair" quality ceiling applied by the caller.
    if context.discipline != "swim":
        return None, None
    active_lengths = [length for length in swim_lengths if length.length_type == "active"]
    if active_lengths:
        return sum(length.duration_s for length in active_lengths), "fit_lengths"
    return float(context.duration_s), "elapsed"


def _decoupling_metric_rows(
    context: ActivityContext,
    power: _PowerChannel,
    hr: _HrChannel,
    run: _RunGap,
) -> tuple[ActivityMetricRow, ...]:
    # -- Decoupling / EF: two halves of equal duration on the "output" series adapted to
    # discipline (regularized power for bike, GAP speed for run). Split by INDEX (not
    # absolute second): power.regularized.values / filtered run series are already
    # "elapsed time" series at 1 sample per useful second (pauses excised) -- an index
    # split is thus a duration-equal split, without new calendar assumptions.
    if context.discipline == "bike" and power.regularized.values:
        values = power.regularized.values
        mid = len(values) // 2
        split_t = (
            power.elapsed[len(power.elapsed) // 2] if power.elapsed else context.duration_s / 2
        )
        hr1, hr2 = _split_hr_by_time(hr, split_t)
        return _load_extra_metric_rows(
            activity_id=context.activity_id,
            discipline="bike",
            output_full=power.np_watts,
            output_half1=normalized_power(values[:mid]),
            output_half2=normalized_power(values[mid:]),
            avg_hr_full=hr.avg_hr_raw,
            avg_hr_half1=average(hr1) if hr1 else None,
            avg_hr_half2=average(hr2) if hr2 else None,
        )
    if context.discipline == "run" and run.speed:
        mid = len(run.speed) // 2
        split_t = context.duration_s / 2
        hr1, hr2 = _split_hr_by_time(hr, split_t)
        return _load_extra_metric_rows(
            activity_id=context.activity_id,
            discipline="run",
            output_full=run.ngp_speed_mps,
            output_half1=normalized_graded_pace_speed_mps(run.speed[:mid], run.grade[:mid]),
            output_half2=normalized_graded_pace_speed_mps(run.speed[mid:], run.grade[mid:]),
            avg_hr_full=hr.avg_hr_raw,
            avg_hr_half1=average(hr1) if hr1 else None,
            avg_hr_half2=average(hr2) if hr2 else None,
        )
    return ()


def _split_hr_by_time(hr: _HrChannel, split_t: float) -> tuple[list[float], list[float]]:
    pairs = zip(hr.elapsed, hr.values, strict=True)
    half1 = [v for e, v in pairs if e < split_t]
    half2 = [v for e, v in zip(hr.elapsed, hr.values, strict=True) if e >= split_t]
    return half1, half2


def build_core_rows(
    decoded: ParsedActivitySource,
    context: ActivityContext,
    *,
    athlete_id: int,
    fit_file_id: int | None,
    threshold: Threshold | None,
    all_threshold: Threshold | None,
    sex: Sex,
) -> CoreActivityBundle:
    """Chain the decoded (L3) and thresholds (L5) into a complete ``CoreActivityBundle``.

    ``context.activity_id`` MUST be consistent with the ``activity_id`` already carried
    by ``decoded`` (supplied to ``decode_fit``/``decode_tcx`` by the caller): a mismatch
    would mean assembling one activity's summary with another's file, one of the most
    costly possible bugs here -- it is detected rather than silently ignored.

    ``sex`` has NO DEFAULT VALUE, deliberately: it selects Banister TRIMP coefficient
    (male 0.64/1.92, female 0.86/1.67). Getting it wrong shifts all training load by
    ~15%, uniformly and silently: the caller must provide it. ``transform.runner`` reads
    it from ``core.athlete``, where a CHECK constraint restricts it to male/female.
    """
    if context.duration_s <= 0:
        msg = f"context.duration_s must be > 0 (got {context.duration_s})"
        raise ValueError(msg)
    samples = decoded.samples
    for sample in samples:
        if sample.activity_id != context.activity_id:
            msg = (
                f"activity_id mismatch: context={context.activity_id} "
                f"sample={sample.activity_id}"
            )
            raise ValueError(msg)

    power = _power_channel(samples)
    hr = _hr_channel(samples)
    run = _run_gap(context.discipline, samples)
    swim_time_s, swim_time_source = _swim_time(context, decoded.swim_lengths)

    cadence_elapsed, cadence_values = _channel(samples, lambda s: s.cadence)
    avg_cadence = average(cadence_values) if cadence_values else None
    speed_elapsed, speed_values = _channel(samples, lambda s: s.speed_mps)
    avg_speed_mps = average(speed_values) if speed_values else None
    distance_candidates = [s.distance_m for s in samples if s.distance_m is not None]
    distance_m = distance_candidates[-1] if distance_candidates else context.distance_m

    data_quality = power.regularized.data_quality
    if context.discipline == "swim" and swim_time_source == "elapsed" and data_quality == "good":
        # Explicit spec ceiling (04 §4.3): without lengths, swim time is an
        # approximation, never "good".
        data_quality = "fair"

    # `core.activity.start_time` is a timestamptz. `context.start_time_local` is a naive
    # datetime in local time: writing it as-is would make PostgreSQL interpret it as UTC,
    # shifting the session by the entire timezone offset unchecked. Fallback is reachable
    # (a TCX without trackpoint timestamps leaves `decoded.start_time` as None), so we
    # reconstruct the absolute instant from the offset the Garmin summary already provides.
    if decoded.start_time is not None:
        activity_start_time = decoded.start_time
    else:
        activity_start_time = context.start_time_local.replace(
            tzinfo=timezone(timedelta(seconds=context.tz_offset_s))
        )
    activity_row = ActivityRow(
        activity_id=context.activity_id,
        athlete_id=athlete_id,
        sport_code=context.sport_code,
        garmin_type_key=context.garmin_type_key,
        start_time=activity_start_time,
        start_time_local=context.start_time_local,
        local_date=context.start_time_local.date(),
        tz_offset_s=context.tz_offset_s,
        duration_s=context.duration_s,
        elapsed_s=context.elapsed_s,
        moving_s=context.moving_s,
        distance_m=distance_m,
        ascent_m=context.ascent_m,
        descent_m=context.descent_m,
        avg_hr=hr.avg_hr,
        max_hr=hr.max_hr,
        avg_power=power.avg_power,
        max_power=power.max_power,
        avg_power_nonzero=power.avg_power_nonzero,
        np_watts=power.np_watts,
        np_reliable=power.np_reliable,
        avg_speed_mps=avg_speed_mps,
        avg_cadence=avg_cadence,
        calories=context.calories,
        ngp_speed_mps=run.ngp_speed_mps,
        gap_applied=run.gap_applied,
        swim_time_s=swim_time_s,
        swim_time_source=swim_time_source,
        total_samples=len(samples),
        imputed_seconds=power.regularized.imputed_seconds,
        excised_seconds=power.regularized.excised_seconds,
        data_quality=data_quality,
        parent_activity_id=context.parent_activity_id,
        is_parent=context.is_parent,
        leg_order=context.leg_order,
        primary_device_id=context.primary_device_id,
        fit_file_id=fit_file_id,
        summary_sha256=context.summary_sha256,
        name=context.name,
        needs_manual_rpe=False,  # computed after needs_manual_rpe(), corrected below.
        source=context.source,
    )

    hr_hist = compute_hr_histogram([s.heart_rate for s in samples])
    hr_histogram_rows = tuple(
        ActivityHrHistogramRow(activity_id=context.activity_id, hr_bpm=bpm, seconds=seconds)
        for bpm, seconds in sorted(hr_hist.items())
    )

    power_curve_rows: tuple[ActivityPowerCurveRow, ...] = ()
    curve_kind = _POWER_CURVE_KIND.get(context.discipline)
    if curve_kind is not None:
        power_curve_rows += _build_power_curve_rows(
            context.activity_id, curve_kind, power.regularized.values
        )
    hr_reg = regularize_series(hr.elapsed, hr.values, fill_value=0.0)
    power_curve_rows += _build_power_curve_rows(context.activity_id, "heart_rate", hr_reg.values)

    load_input = ActivityLoadInput(
        activity_id=context.activity_id,
        sport_code=context.sport_code,
        discipline=context.discipline,
        duration_s=float(context.duration_s),
        distance_m=distance_m,
        swim_time_s=swim_time_s,
        np_watts=power.np_watts,
        ngp_speed_mps=run.ngp_speed_mps,
        hr_histogram=hr_hist,
        threshold=threshold,
        all_threshold=all_threshold,
        sex=sex,
    )
    metric_rows = build_activity_metric_rows(load_input)
    metric_rows += _decoupling_metric_rows(context, power, hr, run)

    activity_row = replace(activity_row, needs_manual_rpe=needs_manual_rpe(metric_rows))

    return CoreActivityBundle(
        activity=activity_row,
        samples=tuple(samples),
        laps=tuple(decoded.laps),
        swim_lengths=tuple(decoded.swim_lengths),
        hr_histogram=hr_histogram_rows,
        power_curve=power_curve_rows,
        metrics=metric_rows,
    )
