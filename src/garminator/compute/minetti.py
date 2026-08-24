"""Grade-adjusted pace (GAP)—Minetti (2002) model, section 3.3 of 04-load-formulas.md.

Running energy cost as a function of grade i (rise/distance, dimensionless—e.g.,
i = 0.05 for 5% grade), 5th-degree polynomial fitted by Minetti et al. (2002):

EC(i) = 155.4·i⁵ - 30.4·i⁴ - 43.3·i³ + 46.3·i² + 19.5·i + 3.6  (J/kg/m)

Experimental validity domain: i ∈ [-0.45, +0.45]. Beyond, the polynomial diverges
and lacks physiological meaning; a practical clamp to ±0.30 (section 3.3.1) is
applied before evaluation, per spec.

Key non-regression property: EC is not monotonic downhill—minimum occurs around
i ≈ -0.18 (beyond that, steep downhill costs more again, eccentric braking). Naive
code assuming "steeper down = cheaper throughout" fails silently; explicitly tested
(see tests with cases from the full Minetti table in spec).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.compute.normalized_power import _rolling_means

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Experimental validity domain of the Minetti (2002) polynomial.
MINETTI_DOMAIN_MIN = -0.45
MINETTI_DOMAIN_MAX = 0.45

#: Practical clamp applied before polynomial evaluation (section 3.3.1).
GRADE_CLAMP = 0.30

#: Polynomial coefficients, descending degree: i^5, i^4, i^3, i^2, i^1, i^0.
_COEFFS = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)


def running_energy_cost(grade: float) -> float:
    """Running energy cost (J/kg/m) for grade (dimensionless).

    grade is clamped to [-GRADE_CLAMP, +GRADE_CLAMP] before evaluation (section 3.3.1):
    beyond that, the polynomial lacks established physiological validity.
    """
    i = max(-GRADE_CLAMP, min(GRADE_CLAMP, grade))
    c5, c4, c3, c2, c1, c0 = _COEFFS
    return c5 * i**5 + c4 * i**4 + c3 * i**3 + c2 * i**2 + c1 * i + c0


def grade_adjustment_factor(grade: float) -> float:
    """Adjustment factor f(i) = EC(i) / EC(0) (dimensionless, > 0)."""
    return running_energy_cost(grade) / running_energy_cost(0.0)


def grade_adjusted_speed_mps(speed_mps: float, grade: float) -> float:
    """Grade-adjusted speed (m/s): v_gap = v x f(i).

    Positive grade (uphill) increases cost, thus adjusted speed; moderate negative
    grade (comfortable downhill) decreases it, to the Minetti model's non-monotone
    minimum, then increases again (see module docstring).
    """
    return speed_mps * grade_adjustment_factor(grade)


def grade_adjusted_pace_s_per_km(speed_mps: float, grade: float) -> float | None:
    """Grade-adjusted pace (s/km). None if adjusted speed is zero or negative."""
    v_gap = grade_adjusted_speed_mps(speed_mps, grade)
    if v_gap <= 0:
        return None
    return 1000.0 / v_gap


def _window_bounds(distance_m: Sequence[float], k: int, half_window_m: float) -> tuple[int, int]:
    """Extreme indices still contained within ±half_window_m around distance_m[k]."""
    n = len(distance_m)
    lo = k
    while lo > 0 and distance_m[k] - distance_m[lo - 1] <= half_window_m:
        lo -= 1
    hi = k
    while hi < n - 1 and distance_m[hi + 1] - distance_m[k] <= half_window_m:
        hi += 1
    return lo, hi


def _smooth_by_distance(
    altitude_m: Sequence[float | None], bounds: Sequence[tuple[int, int]]
) -> list[float | None]:
    """Rolling average of altitude over a **distance** window (section 3.3.2, step 1).

    None for a sample whose window contains no known altitude; intercalated None
    values are ignored in the average, never counted as 0 m.
    """
    smoothed: list[float | None] = []
    for lo, hi in bounds:
        known = [a for a in altitude_m[lo : hi + 1] if a is not None]
        smoothed.append(sum(known) / len(known) if known else None)
    return smoothed


def smooth_grade_series(
    distance_m: Sequence[float],
    altitude_m: Sequence[float | None],
    *,
    half_window_m: float = 10.0,
) -> list[float | None]:
    """Smoothed grade (fraction, e.g., 0.05 = 5%) by centered difference over distance window.

    Unlike temporal smoothing, the window is defined in meters traveled (±half_window_m,
    default 10 m each side—section 3.3.2): at rest, temporal window captures zero
    displacement and undefined grade; distance window naturally skips stationary samples.

    Section 3.3.2 procedure requires two steps, in order:

    1. Smooth altitude by rolling average over the same distance window (±half_window_m)—
       barometric altimetry is noisy at ±0.5 m; raw difference over ~20 m yields grade
       noise of several percent.
    2. Centered difference of smoothed altitude between window bounds.

    For sample k, lo is the farthest-back point still in the window
    (distance_m[k] - distance_m[lo] <= half_window_m, else first available), hi is
    its forward mirror. Grade is (alt_smoothed[hi] - alt_smoothed[lo]) /
    (distance_m[hi] - distance_m[lo]), clamped to ±0.30 (section 3.3.2, aligned with
    core.sample_derived.grade_pct constraint [-30, 30]).

    None if altitude is missing, window has no usable altitude, or available distance
    span < 1 m (rest: undefined grade, never guessed).
    """
    n = len(distance_m)
    if len(altitude_m) != n:
        msg = "distance_m and altitude_m must have the same length"
        raise ValueError(msg)

    bounds = [_window_bounds(distance_m, k, half_window_m) for k in range(n)]
    smoothed = _smooth_by_distance(altitude_m, bounds)

    result: list[float | None] = [None] * n
    for k in range(n):
        if altitude_m[k] is None:
            continue

        lo, hi = bounds[k]
        alt_lo = smoothed[lo]
        alt_hi = smoothed[hi]
        if alt_lo is None or alt_hi is None:
            continue

        span = distance_m[hi] - distance_m[lo]
        if span < 1.0:
            continue

        grade = (alt_hi - alt_lo) / span
        result[k] = max(-GRADE_CLAMP, min(GRADE_CLAMP, grade))

    return result


def normalized_graded_pace_speed_mps(
    speed_mps: Sequence[float], grade: Sequence[float | None], *, window_s: int = 30
) -> float | None:
    """Normalized Graded Pace (NGP), expressed as speed (m/s)—same algorithm as NP.

    Applies grade factor to each speed sample (None grades treated as zero grade,
    f(0) = 1—conservative in absence of altitude), then reuses NP algorithm (30-s
    rolling average, 4th power, 4th root) on adjusted speed series (section 3.3.3).

    Deliberate asymmetry with NP (section 3.3.4): zero or negative speed samples
    are excluded before windowing, not kept like coasting zeros in section 1.4.
    On bike, p = 0 means "coasting in motion"; on run, v = 0 means "not running"
    (traffic light, stop)—keeping them pulls NGP down without physiological content.
    Accepted consequence: 30-s windows can span a stop, exactly like pause excision.

    None if fewer than window_s usable samples (same rule as NP, section 1.6)—
    never 0.
    """
    if len(speed_mps) != len(grade):
        msg = "speed_mps and grade must have the same length"
        raise ValueError(msg)

    v_gap = [
        grade_adjusted_speed_mps(v, g if g is not None else 0.0)
        for v, g in zip(speed_mps, grade, strict=True)
        if v > 0
    ]
    means = _rolling_means(v_gap, window_s)
    if not means:
        return None
    fourth_power_mean = sum(m**4 for m in means) / len(means)
    return float(fourth_power_mean**0.25)


def normalized_graded_pace_s_per_km(
    speed_mps: Sequence[float], grade: Sequence[float | None], *, window_s: int = 30
) -> float | None:
    """NGP expressed as pace (s/km).

    None if NGP speed is None, zero, or negative.
    """
    ngp_speed = normalized_graded_pace_speed_mps(speed_mps, grade, window_s=window_s)
    if ngp_speed is None or ngp_speed <= 0:
        return None
    return 1000.0 / ngp_speed
