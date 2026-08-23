"""TSS/rTSS/sTSS formulas (04-formules-charge.md §2-4).

Pure functions: duration, measured quantity (power or speed), threshold in; intensity
factor (IF) and TSS out. Functions never recalculate NP, GAP, or critical power model—
produced by L4 (``garminator.compute``, parallel development) and received here as plain
``float | None`` (L4 -> L5 contract).

Invariant checked by construction (04 §2.2), except swimming:
    TSS = duration_h x IF^2 x 100        (IF = quantity / threshold)
Swimming uses exponent 3 by TrainingPeaks convention (04 §4.3, configurable).
"""

from __future__ import annotations

from dataclasses import dataclass

_SECONDS_PER_HOUR = 3600.0
_TSS_SCALE = 100.0
_POWER_PACE_EXPONENT = 2
_SWIM_DEFAULT_EXPONENT = 3


@dataclass(frozen=True, slots=True)
class IntensityResult:
    """Result of IF/TSS calculation. Both fields None together or both present: IF always
    None when TSS is, never vice versa (04 §2.4, TSS-4)."""

    intensity_factor: float | None
    tss: float | None


def _duration_hours(duration_s: float | None) -> float | None:
    if duration_s is None or duration_s <= 0:
        return None
    return duration_s / _SECONDS_PER_HOUR


def _tss_from_ratio(
    duration_s: float | None,
    numerator: float | None,
    threshold: float | None,
    exponent: int,
) -> IntensityResult:
    """Common kernel for bike/rTSS-P/rTSS-GAP: IF = numerator/threshold,
    TSS = duration_h x IF^n x 100.

    Never guess missing threshold (non-negotiable rule, addendum §5): threshold None
    or ≤ 0 returns (None, None), never approximation.
    """
    hours = _duration_hours(duration_s)
    if hours is None or numerator is None or threshold is None or threshold <= 0:
        return IntensityResult(None, None)
    if numerator < 0:
        return IntensityResult(None, None)
    intensity_factor = numerator / threshold
    tss = hours * (intensity_factor**exponent) * _TSS_SCALE
    return IntensityResult(intensity_factor, tss)


def bike_tss(
    duration_s: float | None, np_watts: float | None, ftp_watts: float | None
) -> IntensityResult:
    """Bike TSS (04 §2). Threshold = FTP in effect on activity date."""
    return _tss_from_ratio(duration_s, np_watts, ftp_watts, _POWER_PACE_EXPONENT)


def run_power_tss(
    duration_s: float | None, np_watts: float | None, rftp_watts: float | None
) -> IntensityResult:
    """rTSS-P, running by power (04 §3.2). rFTP is DISTINCT threshold from bike FTP
    (never interchangeable, see CHECK constraints in core.threshold); while rFTP is NULL
    for this athlete, method returns (None, None) and method resolution falls to run_gap
    then hr_hrtss."""
    return _tss_from_ratio(duration_s, np_watts, rftp_watts, _POWER_PACE_EXPONENT)


def run_gap_tss(
    duration_s: float | None,
    ngp_speed_mps: float | None,
    ftpa_speed_mps: float | None,
) -> IntensityResult:
    """rTSS-GAP, running by grade-adjusted pace (04 §3.3). ``ngp_speed_mps`` is
    grade-adjusted normalized speed (NGP) from L4 (Minetti polynomial, outside L5 scope);
    ``ftpa_speed_mps`` is threshold speed (see
    :func:`garminator.load.thresholds.pace_s_per_km_to_speed_mps`)."""
    return _tss_from_ratio(duration_s, ngp_speed_mps, ftpa_speed_mps, _POWER_PACE_EXPONENT)


def swim_tss(
    swim_time_s: float | None,
    distance_m: float | None,
    css_m_s: float | None,
    exponent: int = _SWIM_DEFAULT_EXPONENT,
) -> IntensityResult:
    """Swimming TSS (04 §4). ``swim_time_s`` MUST be active swim time
    (``core.activity.swim_time_s``, sum of active lengths), never elapsed time (04 §4.4)—
    caller's responsibility."""
    hours = _duration_hours(swim_time_s)
    if (
        hours is None
        or swim_time_s is None
        or distance_m is None
        or distance_m <= 0
        or css_m_s is None
        or css_m_s <= 0
    ):
        return IntensityResult(None, None)
    speed_mps = distance_m / swim_time_s
    intensity_factor = speed_mps / css_m_s
    tss = hours * (intensity_factor**exponent) * _TSS_SCALE
    return IntensityResult(intensity_factor, tss)
