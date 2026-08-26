"""Tests for t0.compute.normalized_power - cases NP-0 to NP-4 of 04-load-formulas.md.

All expected numeric values are transcribed from the specification.
"""

from __future__ import annotations

from t0.compute.normalized_power import (
    RELIABLE_THRESHOLD_S,
    average,
    average_nonzero,
    normalized_power,
    np_reliable,
)


def test_np0_fewer_than_30_samples_is_none() -> None:
    """NP-0: a series shorter than 30 s defines no full window -> None, never 0."""
    p = [200.0] * 29
    assert normalized_power(p) is None


def test_np0_degenerate_constant_series() -> None:
    """NP-0: constant 3600 s series -> NP = avg = 250.0 exactly (§1.6)."""
    p = [250.0] * 3600
    assert normalized_power(p) == 250.0
    assert average(p) == 250.0


def test_np0_exactly_30_samples_is_defined() -> None:
    p = [200.0] * 30
    assert normalized_power(p) == 200.0


def test_np1_hand_traced_algorithm_mechanics() -> None:
    """NP-1: W=2 (window shrunk so the result stays hand-checkable), §1.6."""
    p = [100.0, 200.0, 300.0, 400.0]
    result = normalized_power(p, window_s=2)
    assert result is not None
    assert abs(result - 283.6449161403191) < 1e-6


def test_np2_two_120s_plateaus() -> None:
    """NP-2: 60 s at 200 W then 60 s at 300 W, standard 30 s window."""
    p = [200.0] * 60 + [300.0] * 60
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 261.029088063548) < 1e-6
    assert average(p) == 250.0


def test_np3_power_then_stop_keeps_the_zeros() -> None:
    """NP-3: 30 s at 300 W then 30 s at 0 W - coasting zeros are kept, not excluded."""
    p = [300.0] * 30 + [0.0] * 30
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 203.09282812902046) < 1e-6
    assert average(p) == 150.0


def test_np4_np_reliable_below_the_threshold() -> None:
    """NP-4: between 30 and 300 usable seconds, NP is defined but unreliable."""
    assert np_reliable(30) is False
    assert np_reliable(RELIABLE_THRESHOLD_S - 1) is False
    assert np_reliable(RELIABLE_THRESHOLD_S) is True
    assert np_reliable(29) is None


def test_average_nonzero_excludes_the_zeros() -> None:
    p = [0.0, 0.0, 100.0, 200.0]
    assert average_nonzero(p) == 150.0
    assert average(p) == 75.0


def test_average_of_an_empty_series_is_none_never_zero() -> None:
    assert average([]) is None
    assert average_nonzero([]) is None


def test_normalized_power_is_falsifiable_power_ordering_matters() -> None:
    """Would fail if the implementation fell back to a plain mean
    (common bug: forgetting the 4th power / 4th root).
    """
    flat = [250.0] * 60
    variable = [100.0] * 30 + [400.0] * 30
    assert average(flat) == average(variable) == 250.0
    np_flat = normalized_power(flat)
    np_variable = normalized_power(variable)
    assert np_flat is not None
    assert np_variable is not None
    assert np_flat == 250.0
    assert np_variable > np_flat


def test_normalized_power_empty_series_is_none() -> None:
    assert normalized_power([]) is None


def test_normalized_power_entirely_zero_series_is_none_never_zero() -> None:
    """§1.5: "all values zero" -> NULL.

    A series at 0 W from start to finish is not an effort at 0 W, it is a missing
    sensor. Returning 0.0 would feed IF = 0 then TSS = 0 into L5 with no downstream
    consumer able to tell that 0 apart from a measured value.
    """
    assert normalized_power([0.0] * 3600) is None
    assert average_nonzero([0.0] * 3600) is None
    # A single nonzero watt is enough to make the series usable.
    series = [0.0] * 3599 + [1.0]
    result = normalized_power(series)
    assert result is not None
    assert result > 0.0


def test_normalized_power_single_sample_is_none() -> None:
    assert normalized_power([250.0]) is None
    assert normalized_power([250.0], window_s=1) == 250.0


def test_np_reliable_does_not_mask_a_too_short_series() -> None:
    """np_reliable must say None (not False) when NP itself is None.

    False would mean "NP exists but is fragile"; None means "there is no NP at all".
    Downstream handles the two differently.
    """
    for count in (0, 1, 29):
        assert np_reliable(count) is None
        assert normalized_power([200.0] * count) is None


def test_np2_edge_convention_a_is_the_one_implemented() -> None:
    """Convention (B) "expanding window" would give 250.016, not 261.029 (§1.6).

    A 4.4% discrepancy: this test would fail if someone "fixed" edge handling by also
    emitting the incomplete windows at the start of the series.
    """
    p = [200.0] * 60 + [300.0] * 60
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 261.029088063548) < 1e-9
    assert abs(result - 250.01614656735006) > 10.0
