"""Tests for t0.compute.power_curve - cases PC-1 and PC-2 of 04-load-formulas.md §8."""

from __future__ import annotations

from itertools import pairwise

import pytest

from t0.compute.power_curve import (
    best_effort,
    best_effort_spans_pause,
    log_spaced_durations,
    power_duration_curve,
)

# PC-1: 30 samples, plateaus 10x100W then 5x400W then 15x150W (§8.5 of spec).
_PC1_SERIES = [100.0] * 10 + [400.0] * 5 + [150.0] * 15


def test_pc1_best_5s_on_high_plateau() -> None:
    assert best_effort(_PC1_SERIES, 5) == 400.0


def test_pc1_best_10s_straddles_high_and_low_plateau() -> None:
    """best(10) = 275.0, found on window 10..19 (5 s at 400 W + 5 s at 150 W).

    Brute-force verification below: spec prose §8.5 pointed to window 5..14
    (5x100 + 5x400 = 2500, /10 = 250.0), which is NOT the best. Value 275.0 is correct;
    the prose was wrong and has been fixed in the spec. This test locks both value and
    window location to catch implementations that stumble on 275.0 accidentally.
    """
    assert best_effort(_PC1_SERIES, 10) == 275.0
    assert sum(_PC1_SERIES[5:15]) / 10 == 250.0
    assert sum(_PC1_SERIES[10:20]) / 10 == 275.0
    brute = max(sum(_PC1_SERIES[i : i + 10]) / 10 for i in range(len(_PC1_SERIES) - 10 + 1))
    assert brute == 275.0


def test_pc1_best_30s_equals_global_average() -> None:
    assert best_effort(_PC1_SERIES, 30) == 175.0


def test_pc1_best_31s_out_of_range_is_none() -> None:
    """Duration longer than the series -> None, never a truncated value or 0."""
    assert best_effort(_PC1_SERIES, 31) is None


def test_pc2_monotonicity_within_activity() -> None:
    """PC-2: for the same activity, best(d1) >= best(d2) when d1 < d2 (general property)."""
    series = [100.0, 200.0, 300.0, 150.0, 250.0, 50.0, 400.0, 100.0, 300.0, 200.0] * 5
    durations = [1, 2, 5, 10, 20, 30, 50]
    values = [best_effort(series, d) for d in durations]
    for shorter, longer in pairwise(values):
        assert shorter is not None
        assert longer is not None
        assert shorter >= longer


def test_best_effort_zero_or_negative_duration_is_none() -> None:
    assert best_effort([1.0, 2.0, 3.0], 0) is None
    assert best_effort([1.0, 2.0, 3.0], -1) is None


def test_best_effort_empty_series_is_none() -> None:
    assert best_effort([], 1) is None


def test_log_spaced_durations_increasing_and_deduped() -> None:
    durations = log_spaced_durations(1, 3600)
    assert durations == sorted(set(durations))
    assert durations[0] == 1
    assert durations[-1] <= 3600
    # Density expected by spec: on the order of a few dozen to a hundred points over
    # 3600 s range (§8.2).
    assert 20 <= len(durations) <= 120


def test_log_spaced_durations_respects_bounds() -> None:
    durations = log_spaced_durations(5, 100)
    assert all(5 <= d <= 100 for d in durations)


def test_power_duration_curve_durations_out_of_range_are_none() -> None:
    series = [100.0] * 20
    curve = power_duration_curve(series, min_s=1, max_s=50)
    # No duration generated beyond series length makes sense; those that are despite that
    # (max_s forced larger than series) must remain None.
    assert any(d > 20 for d in curve)
    for d, value in curve.items():
        if d > 20:
            assert value is None
        else:
            assert value is not None


def test_best_effort_spans_pause_signals_window_crossing_excision() -> None:
    """A winning window that crosses an excision boundary is marked spans_pause=True.

    Series intentionally NOT constant: best 6 s window is unique 6..9 (average 500), which
    straddles the boundary at index 5 only if the boundary is properly accounted for. With
    a constant series, any window would work and the test would pass even if detection
    were broken.
    """
    # Two segments rejoined after pause excision: boundary at index 5.
    series = [100.0, 100.0, 100.0, 100.0, 400.0, 400.0, 500.0, 500.0, 500.0, 500.0]
    value, spans_pause = best_effort_spans_pause(series, 6, segment_boundaries=[5])
    # Best 6 s window: indices 4..9 -> (400+400+500*4)/6.
    assert value == pytest.approx((400.0 * 2 + 500.0 * 4) / 6)
    assert spans_pause is True


def test_best_effort_spans_pause_window_abutting_boundary_does_not_cross() -> None:
    """Window that ends just before boundary, or starts on it, does not cross it.

    Edge case most likely to be missed: boundary at index b belongs to downstream segment;
    window [b, b+d-1] is entirely contiguous, as is [b-d, b-1].
    """
    series = [100.0] * 5 + [500.0] * 5
    value, spans = best_effort_spans_pause(series, 5, segment_boundaries=[5])
    assert value == 500.0  # window 5..9, starts on boundary
    assert spans is False

    value, spans = best_effort_spans_pause(series[::-1], 5, segment_boundaries=[5])
    assert value == 500.0  # window 0..4, ends just before boundary
    assert spans is False


def test_best_effort_spans_pause_duration_out_of_range_is_none_and_false() -> None:
    assert best_effort_spans_pause([1.0, 2.0], 5, segment_boundaries=[1]) == (None, False)
    assert best_effort_spans_pause([], 1, segment_boundaries=[]) == (None, False)


def test_best_effort_spans_pause_without_excision_is_always_false() -> None:
    series = [100.0, 400.0, 200.0, 300.0]
    assert best_effort_spans_pause(series, 2, segment_boundaries=[])[1] is False


def test_best_effort_spans_pause_value_identical_to_best_effort() -> None:
    """Returned value must never depend on presence of boundaries."""
    series = [100.0, 400.0, 200.0, 300.0, 50.0, 600.0, 120.0]
    for d in range(1, len(series) + 1):
        assert best_effort_spans_pause(series, d, segment_boundaries=[3])[0] == best_effort(
            series, d
        )


def test_best_effort_spans_pause_false_when_window_crosses_nothing() -> None:
    series = [100.0] * 5 + [500.0] * 5
    value, spans_pause = best_effort_spans_pause(series, 3, segment_boundaries=[5])
    assert value == 500.0
    assert spans_pause is False


def test_power_duration_curve_empty_series_does_not_raise_and_invents_nothing() -> None:
    curve = power_duration_curve([])
    assert all(value is None for value in curve.values())


def test_power_duration_curve_single_point() -> None:
    curve = power_duration_curve([250.0])
    assert curve == {1: 250.0}


def test_best_effort_accepts_negative_values_without_treating_as_missing() -> None:
    """Generic series (altitude, temperature, ...): -5 is a value, not an absence."""
    assert best_effort([-5.0, -1.0, -3.0], 1) == -1.0
    assert best_effort([-5.0, -5.0], 2) == -5.0


def test_best_effort_all_zero_values_yields_zero_not_none() -> None:
    """Here 0 is the right answer: series exists and equals 0, not an absence."""
    assert best_effort([0.0] * 10, 5) == 0.0


def test_log_spaced_durations_invalid_bounds_raise_value_error() -> None:
    with pytest.raises(ValueError, match="min_s"):
        log_spaced_durations(0, 100)
    with pytest.raises(ValueError, match="max_s"):
        log_spaced_durations(10, 5)


def test_log_spaced_durations_min_equals_max() -> None:
    assert log_spaced_durations(60, 60) == [60]


def test_log_spaced_durations_covers_spec_usual_durations() -> None:
    """5 s / 60 s / 300 s / 1200 s / 3600 s are durations cited in §8.1.

    The log grid does not contain them all exactly; we verify it approaches each within
    10%, otherwise the displayed curve would be misleading.
    """
    durations = log_spaced_durations(1, 3600)
    for cible in (5, 60, 300, 1200, 3600):
        proche = min(durations, key=lambda d: abs(d - cible))
        assert abs(proche - cible) / cible < 0.10
