"""Tests for garminator.compute.regularization: gaps, interpolation, pause excision.

No self-contained numeric cases in 04-load-formulas.md for this module (it is a
mechanical prerequisite to §1.3 algorithms, not a formula on its own); tests below
transcribe the behavior spelled out explicitly by the specification and are designed
to fail if the behavior is absent or incorrect.
"""

from __future__ import annotations

import math

import pytest

from garminator.compute.regularization import Gap, detect_gaps, regularize_series


def test_detect_gaps_no_gap_on_continuous_series() -> None:
    assert detect_gaps([0, 1, 2, 3, 4]) == []


def test_detect_gaps_finds_single_gap() -> None:
    gaps = detect_gaps([0, 1, 2, 7, 8])
    assert gaps == [Gap(start_s=3, end_s=6)]
    assert gaps[0].length_s == 4


def test_detect_gaps_multiple_gaps() -> None:
    gaps = detect_gaps([0, 5, 6, 20])
    assert gaps == [Gap(start_s=1, end_s=4), Gap(start_s=7, end_s=19)]


def test_detect_gaps_raises_value_error_if_not_strictly_increasing() -> None:
    """Duplicate or backward instant is a programming error, not a gap."""
    with pytest.raises(ValueError, match="strictly increasing"):
        detect_gaps([0, 1, 1, 2])
    with pytest.raises(ValueError, match="strictly increasing"):
        detect_gaps([0, 2, 1])


def test_regularize_series_short_gap_is_linearly_interpolated() -> None:
    """Gap of 2 s (<=3 s) between 100 and 200 -> two intermediate points interpolated."""
    result = regularize_series([0, 3], [100.0, 200.0])
    assert result.total_seconds == 4
    expected = (100.0, 133.33333333333334, 166.66666666666666, 200.0)
    assert result.values == pytest.approx(expected)
    assert result.imputed == (False, True, True, False)
    assert result.imputed_seconds == 2
    assert result.excised_seconds == 0


def test_regularize_series_long_gap_outside_pause_imputed_to_zero() -> None:
    """Gap of 5 s (>3 s) with no pause interval -> conservative imputation to 0."""
    result = regularize_series([0, 5], [200.0, 300.0])
    assert result.total_seconds == 6
    assert result.values == (200.0, 0.0, 0.0, 0.0, 0.0, 300.0)
    assert result.imputed == (False, True, True, True, True, False)
    assert result.imputed_seconds == 4
    assert result.excised_seconds == 0


def test_regularize_series_long_gap_within_pause_is_excised() -> None:
    """Gap of 5 s entirely covered by a pause -> excised, no seconds created."""
    result = regularize_series([0, 5], [200.0, 300.0], pause_intervals=[(1, 4)])
    assert result.total_seconds == 2
    assert result.values == (200.0, 300.0)
    assert result.imputed == (False, False)
    assert result.imputed_seconds == 0
    assert result.excised_seconds == 4


def test_regularize_series_partial_pause_is_not_excised() -> None:
    """Pause interval not covering entire gap -> no excision, normal imputation."""
    result = regularize_series([0, 5], [200.0, 300.0], pause_intervals=[(1, 2)])
    assert result.excised_seconds == 0
    assert result.imputed_seconds == 4


def test_regularize_series_quality_poor_beyond_10_percent_imputed() -> None:
    # 20 s series, 3 s imputed (gap of 3 s > 10% threshold -> poor).
    result = regularize_series([0, 4, 24], [100.0, 100.0, 100.0])
    assert result.imputed_seconds / result.total_seconds > 0.10
    assert result.data_quality == "poor"


def test_regularize_series_quality_good_under_10_percent_imputed() -> None:
    result = regularize_series([0, *range(2, 101)], [100.0] * 100)
    assert result.imputed_seconds / result.total_seconds <= 0.10
    assert result.data_quality == "good"


def test_regularize_series_empty_yields_quality_unknown() -> None:
    result = regularize_series([], [])
    assert result.total_seconds == 0
    assert result.data_quality == "unknown"
    assert result.values == ()


def test_regularize_series_inconsistent_lengths_raise_value_error() -> None:
    with pytest.raises(ValueError, match="same length"):
        regularize_series([0, 1], [100.0])


def test_regularize_series_exposes_boundary_indices_for_power_curve() -> None:
    """Without this field, best_effort_spans_pause has no source for boundaries.

    Gap of 10 s entirely covered by pause -> excision: following sample is first of new
    segment, non-contiguous in real time from its predecessor.
    """
    result = regularize_series(
        [0, 1, 2, 13, 14], [10.0, 11.0, 12.0, 13.0, 14.0], pause_intervals=[(3, 12)]
    )
    assert result.excised_seconds == 10
    assert result.values == (10.0, 11.0, 12.0, 13.0, 14.0)
    assert result.segment_boundaries == (3,)
    # Boundary points to first sample after the pause.
    assert result.values[result.segment_boundaries[0]] == 13.0


def test_regularize_series_multiple_excisions_yield_multiple_boundaries() -> None:
    result = regularize_series(
        [0, 10, 11, 21],
        [1.0, 2.0, 3.0, 4.0],
        pause_intervals=[(1, 9), (12, 20)],
    )
    assert result.segment_boundaries == (1, 3)
    assert result.excised_seconds == 18
    assert result.total_seconds == 4


def test_regularize_series_without_pause_no_boundary() -> None:
    assert regularize_series([0, 1, 2], [1.0, 2.0, 3.0]).segment_boundaries == ()
    # Imputed gap (not excised) is NOT a boundary: series remains contiguous.
    assert regularize_series([0, 5], [1.0, 2.0]).segment_boundaries == ()


def test_regularize_series_3s_gap_is_upper_bound_interpolated() -> None:
    """max_interp_gap_s=3: a 3 s gap is interpolated, a 4 s gap is not."""
    trois = regularize_series([0, 4], [0.0, 4.0])
    assert trois.values == (0.0, 1.0, 2.0, 3.0, 4.0)
    quatre = regularize_series([0, 5], [0.0, 5.0])
    assert quatre.values == (0.0, 0.0, 0.0, 0.0, 0.0, 5.0)


def test_regularize_series_long_gap_never_interpolated() -> None:
    """A 60 s gap must be filled with fill_value, never interpolated as ramp."""
    result = regularize_series([0, 61], [100.0, 400.0])
    assert result.total_seconds == 62
    assert set(result.values[1:61]) == {0.0}
    assert result.imputed_seconds == 60
    assert result.data_quality == "poor"


def test_regularize_series_custom_fill_value() -> None:
    """fill_value is not hardcoded to 0: a heart rate series is not imputed to 0 bpm."""
    result = regularize_series([0, 5], [140.0, 150.0], fill_value=math.nan)
    assert all(math.isnan(v) for v in result.values[1:5])
    assert result.values[0] == 140.0
    assert result.values[5] == 150.0


def test_regularize_series_single_sample() -> None:
    result = regularize_series([7], [200.0])
    assert result.values == (200.0,)
    assert result.total_seconds == 1
    assert result.data_quality == "good"
    assert result.segment_boundaries == ()


def test_regularize_series_declared_pause_on_short_gap_does_not_override_interpolation() -> None:
    """§1.3: order is mandatory — <=3 s interpolated BEFORE any pause test."""
    result = regularize_series([0, 3], [100.0, 200.0], pause_intervals=[(1, 2)])
    assert result.excised_seconds == 0
    assert result.total_seconds == 4


def test_regularize_series_non_increasing_time_raises_value_error() -> None:
    """Backward or repeated instant is a caller error, never a gap.

    Without this safeguard, gap = curr_t - prev_t - 1 would become negative and the
    interpolation loop would be silently skipped: series would exit shorter than expected
    with no signal.
    """
    with pytest.raises(ValueError, match="strictly increasing"):
        regularize_series([0, 5, 5], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="strictly increasing"):
        regularize_series([0, 5, 3], [1.0, 2.0, 3.0])
