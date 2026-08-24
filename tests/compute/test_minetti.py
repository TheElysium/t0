"""Tests for garminator.compute.minetti - Minetti table and cases GAP-1/GAP-2.

The numeric values in the table below are computed directly from the specification
polynomial (04-load-formulas.md §3.3) with pytest.approx to tolerate float rounding;
cases GAP-1/GAP-2 transcribe the bounded values spelled out by the specification.
"""

from __future__ import annotations

import pytest

from garminator.compute.minetti import (
    grade_adjusted_pace_s_per_km,
    grade_adjusted_speed_mps,
    grade_adjustment_factor,
    normalized_graded_pace_s_per_km,
    normalized_graded_pace_speed_mps,
    running_energy_cost,
    smooth_grade_series,
)

# (grade i, expected EC(i) J/kg/m, expected f(i)) -- recomputed from the spec polynomial.
_MINETTI_TABLE: list[tuple[float, float, float]] = [
    (-0.30, 2.4622379999999997, 0.6839549999999999),
    (-0.25, 2.0248046875, 0.5624457465277778),
    (-0.20, 1.8000320000000003, 0.500008888888889),
    (-0.15, 1.8356968125000002, 0.50991578125),
    (-0.10, 2.151706, 0.5976961111111111),
    (-0.05, 2.7459239374999997, 0.7627566493055554),
    (0.00, 3.6, 1.0),
    (0.05, 4.6851960625, 1.3014433506944445),
    (0.10, 5.968214, 1.6578372222222222),
    (0.15, 7.4170231875, 2.06028421875),
    (0.20, 9.006688, 2.501857777777778),
    (0.25, 10.7251953125, 2.979220920138889),
    (0.30, 12.579282, 3.494245),
]


@pytest.mark.parametrize(("grade", "expected_ec", "expected_f"), _MINETTI_TABLE)
def test_running_energy_cost_and_grade_adjustment_factor(
    grade: float, expected_ec: float, expected_f: float
) -> None:
    assert running_energy_cost(grade) == pytest.approx(expected_ec)
    assert grade_adjustment_factor(grade) == pytest.approx(expected_f)


def test_ec_at_zero_is_the_reference_cost() -> None:
    assert running_energy_cost(0.0) == pytest.approx(3.6)
    assert grade_adjustment_factor(0.0) == pytest.approx(1.0)


def test_ec_is_non_monotonic_downhill_with_a_minimum_near_minus_0_18() -> None:
    """Key non-regression property: downhill is not always cheaper.

    A naive "the steeper the descent the cheaper" implementation would fail here: cost
    rises again beyond about -18% grade (eccentric braking, Minetti 2002).
    """
    assert running_energy_cost(-0.30) > running_energy_cost(-0.20)
    assert running_energy_cost(-0.30) > running_energy_cost(-0.15)


def test_practical_clamp_at_0_30_beyond_the_domain() -> None:
    """A grade beyond the practical clamp is brought back to ±0.30 before evaluation (§3.3.1)."""
    assert running_energy_cost(0.50) == pytest.approx(running_energy_cost(0.30))
    assert running_energy_cost(-0.50) == pytest.approx(running_energy_cost(-0.30))


def test_gap1_positive_grade_of_5_percent() -> None:
    """GAP-1: v=3.7037037... m/s (4:30/km pace), +5% grade -> bounded values from the spec."""
    v = 1000.0 / 270.0  # 4:30 min/km = 270 s/km
    f = grade_adjustment_factor(0.05)
    assert f == pytest.approx(1.3014433506944445)

    v_gap = grade_adjusted_speed_mps(v, 0.05)
    assert v_gap == pytest.approx(4.820160558127572)

    pace = grade_adjusted_pace_s_per_km(v, 0.05)
    assert pace == pytest.approx(207.4619689408142)


def test_gap2_comfortable_negative_grade_lowers_the_gap_speed() -> None:
    """GAP-2: a moderate negative grade (inside the beneficial zone) gives v_gap < v."""
    v = 3.0
    v_gap = grade_adjusted_speed_mps(v, -0.05)
    assert v_gap < v


def test_grade_adjusted_pace_is_none_when_adjusted_speed_is_not_positive() -> None:
    assert grade_adjusted_pace_s_per_km(0.0, 0.0) is None


def test_smooth_grade_series_flat_terrain_gives_zero_grade() -> None:
    distance = [float(i) for i in range(60)]
    altitude = [100.0] * 60
    grades = smooth_grade_series(distance, altitude)
    non_none = [g for g in grades if g is not None]
    assert non_none
    assert all(abs(g) < 1e-9 for g in non_none)


def test_smooth_grade_series_linear_climb_gives_a_constant_positive_grade() -> None:
    distance = [float(i) for i in range(100)]
    altitude = [i * 0.5 for i in range(100)]  # 0.5 m of gain per meter -> 50% grade
    grades = smooth_grade_series(distance, altitude)
    center = grades[50]
    assert center is not None
    assert center == pytest.approx(0.30)  # clamped to 0.30, real grade = 0.5


def test_smooth_grade_series_at_rest_gives_none() -> None:
    """At rest (constant distance) the grade is undefined -> None, never guessed."""
    distance = [0.0] * 30
    altitude = [100.0] * 30
    grades = smooth_grade_series(distance, altitude)
    assert all(g is None for g in grades)


def test_smooth_grade_series_missing_altitude_gives_none() -> None:
    distance = [float(i) for i in range(20)]
    altitude: list[float | None] = [None] * 20
    grades = smooth_grade_series(distance, altitude)
    assert all(g is None for g in grades)


def test_normalized_graded_pace_fewer_than_30_samples_is_none() -> None:
    speed = [3.0] * 29
    grade = [0.0] * 29
    assert normalized_graded_pace_speed_mps(speed, grade) is None
    assert normalized_graded_pace_s_per_km(speed, grade) is None


def test_normalized_graded_pace_on_flat_equals_plain_speed_np() -> None:
    speed = [3.0] * 60
    grade = [0.0] * 60
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    assert ngp == pytest.approx(3.0)


def test_normalized_graded_pace_none_grade_is_treated_as_flat() -> None:
    speed = [3.0] * 60
    grade: list[float | None] = [None] * 60
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    assert ngp == pytest.approx(3.0)


def test_model_minimum_is_located_near_minus_0_1814() -> None:
    """Numeric location of the minimum: i ~ -0.1814, EC ~ 1.78127 (§3.3.1).

    Recomputed here by fine sweep rather than trusting the transcribed table: this is
    the non-trivial property of the model (eccentric braking on steep descents).
    """
    grid = [-0.30 + k * 0.0001 for k in range(2501)]  # -0.30 .. -0.05
    i_min = min(grid, key=running_energy_cost)
    assert i_min == pytest.approx(-0.1814, abs=5e-4)
    assert running_energy_cost(i_min) == pytest.approx(1.78127, abs=1e-4)
    # f(-0.20) = 0.5000 is the convenient control point given by the spec.
    assert grade_adjustment_factor(-0.20) == pytest.approx(0.5, abs=1e-4)


def test_clamp_is_a_plateau_not_an_extrapolation() -> None:
    """Beyond ±0.30 the polynomial is no longer evaluated: the factor becomes constant.

    The unclamped polynomial would give EC(0.45) = 19.43 against EC(0.30) = 12.58, i.e.
    +54%: if the clamp ever disappears, this test reports it.
    """
    assert running_energy_cost(0.45) == running_energy_cost(0.30)
    assert running_energy_cost(10.0) == running_energy_cost(0.30)
    assert running_energy_cost(-10.0) == running_energy_cost(-0.30)
    unclamped_polynomial = (
        155.4 * 0.45**5 - 30.4 * 0.45**4 - 43.3 * 0.45**3 + 46.3 * 0.45**2 + 19.5 * 0.45 + 3.6
    )
    assert unclamped_polynomial == pytest.approx(19.4260145625)
    assert running_energy_cost(0.45) < unclamped_polynomial


def test_normalized_graded_pace_excludes_zero_speed_samples() -> None:
    """§3.3.4: v = 0 (red light) is not the equivalent of p = 0 (coasting).

    Keeping them would drag NGP down with no physiological content. Deliberate asymmetry
    with §1.4, tested here: inserting stops must not change NGP.
    """
    speed = [3.0] * 60
    grade: list[float | None] = [0.0] * 60
    reference = normalized_graded_pace_speed_mps(speed, grade)
    assert reference == pytest.approx(3.0)

    with_stops = [3.0] * 30 + [0.0] * 20 + [3.0] * 30
    grade_with_stops: list[float | None] = [0.0] * 80
    assert normalized_graded_pace_speed_mps(with_stops, grade_with_stops) == pytest.approx(3.0)


def test_normalized_graded_pace_fewer_than_30_usable_samples_is_none() -> None:
    """40 samples of which 20 at rest -> 20 usable -> None, not a fabricated NGP."""
    speed = [3.0] * 20 + [0.0] * 20
    grade: list[float | None] = [0.0] * 40
    assert normalized_graded_pace_speed_mps(speed, grade) is None
    assert normalized_graded_pace_s_per_km(speed, grade) is None


def test_normalized_graded_pace_empty_and_mismatched_series() -> None:
    assert normalized_graded_pace_speed_mps([], []) is None
    with pytest.raises(ValueError, match="same length"):
        normalized_graded_pace_speed_mps([3.0], [])


def test_normalized_graded_pace_climb_raises_the_ngp() -> None:
    """Falsifiability: at equal speed, a climb must raise NGP (f > 1)."""
    speed = [3.0] * 60
    flat: list[float | None] = [0.0] * 60
    climb: list[float | None] = [0.10] * 60
    ngp_flat = normalized_graded_pace_speed_mps(speed, flat)
    ngp_climb = normalized_graded_pace_speed_mps(speed, climb)
    assert ngp_flat is not None
    assert ngp_climb is not None
    assert ngp_climb == pytest.approx(3.0 * 1.6578372222222222)
    assert ngp_climb > ngp_flat
    # ... and the matching pace must be FASTER (fewer s/km).
    pace_flat = normalized_graded_pace_s_per_km(speed, flat)
    pace_climb = normalized_graded_pace_s_per_km(speed, climb)
    assert pace_flat is not None
    assert pace_climb is not None
    assert pace_climb < pace_flat


def test_smooth_grade_series_smooths_out_altimetric_noise() -> None:
    """§3.3.2 step 1: altitude must be averaged before the centered difference.

    Flat terrain + sawtooth barometric noise of ±0.5 m. Without smoothing, the raw
    difference alt[hi]-alt[lo] is ±1 m over ~20 m, i.e. ±5% of grade. With smoothing,
    the noise almost cancels out.
    """
    distance = [float(i) for i in range(80)]
    altitude: list[float | None] = [100.0 + (0.5 if i % 2 == 0 else -0.5) for i in range(80)]
    grades = smooth_grade_series(distance, altitude)
    center = [g for g in grades[20:60] if g is not None]
    assert len(center) == 40
    assert max(abs(g) for g in center) < 0.01

    # Proof that the noise is really there and the test is not vacuous:
    # the RAW difference over the same window is 1 m over 20 m, i.e. 5%.
    alt_hi, alt_lo = altitude[30], altitude[9]
    assert alt_hi is not None
    assert alt_lo is not None
    raw = (alt_hi - alt_lo) / (distance[30] - distance[9])
    assert abs(raw) == pytest.approx(1.0 / 21.0, abs=1e-9)


def test_smooth_grade_series_mismatched_lengths_raise_value_error() -> None:
    with pytest.raises(ValueError, match="same length"):
        smooth_grade_series([0.0, 1.0], [100.0])


def test_smooth_grade_series_empty_series() -> None:
    assert smooth_grade_series([], []) == []


def test_smooth_grade_series_partially_missing_altitude() -> None:
    """An altitude hole must not be counted as 0 m in the rolling average."""
    distance = [float(i) for i in range(60)]
    altitude: list[float | None] = [float(i) * 0.05 for i in range(60)]
    altitude[30] = None
    grades = smooth_grade_series(distance, altitude)
    assert grades[30] is None  # altitude missing at this sample -> grade unknown
    neighbor = grades[20]
    assert neighbor is not None
    assert neighbor == pytest.approx(0.05, abs=5e-3)


def test_smooth_grade_series_isolated_altitude_stays_usable() -> None:
    """A single known altitude is enough: lo and hi are within half_window_m of k, so
    their own windows contain k and the smoothed average is defined there.

    Consequence: the grade is 0 (same smoothed altitude at both bounds), which is the
    correct information - no elevation change is measured between the bounds.
    """
    distance = [float(i) * 5.0 for i in range(20)]
    altitude: list[float | None] = [None] * 20
    altitude[10] = 100.0
    grades = smooth_grade_series(distance, altitude, half_window_m=10.0)
    assert grades[10] == pytest.approx(0.0)
    assert grades[0] is None  # altitude missing at this sample
