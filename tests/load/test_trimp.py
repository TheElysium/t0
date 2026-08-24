"""Self-contained test cases from 04-load-formulas.md §5.1 (Banister TRIMP)."""

from __future__ import annotations

import math

from garminator.load.trimp import trimp_from_histogram


def _histogram_constant(hr_bpm: int, seconds: int) -> dict[int, int]:
    return {hr_bpm: seconds}


class TestTrimp:
    def test_trimp_1_male(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is not None
        assert math.isclose(value, 95.88495346861073, rel_tol=1e-9)

    def test_trimp_2_female_different_coefficient(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="female")
        assert value is not None
        assert math.isclose(value, 108.74116520702313, rel_tol=1e-9)
        # Falsifiability: female variant must differ from male variant, otherwise sex
        # parameter would not be actually used in the calculation.
        male_value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value != male_value

    def test_trimp_3_at_lthr_threshold(self) -> None:
        """1h at LTHR (170 bpm) yields ~170 TRIMP, NOT 100: the key point of the spec is
        that TRIMP lives on a different scale from TSS and must never be summed with it
        in the same load column."""
        histogram = _histogram_constant(170, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is not None
        assert math.isclose(value, 170.651090478826, rel_tol=1e-9)

    def test_hr_absent_yields_none_never_zero(self) -> None:
        """Non-negotiable business rule: without usable HR, TRIMP must be None, never 0.0
        — a zero would be read as 'effortless session'."""
        value = trimp_from_histogram({}, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is None

    def test_thresholds_absent_yields_none_never_zero(self) -> None:
        histogram = _histogram_constant(145, 3600)
        assert trimp_from_histogram(histogram, hr_rest_bpm=None, hr_max_bpm=190) is None
        assert trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=None) is None

    def test_incoherent_hr_max_yields_none(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=190, hr_max_bpm=50)
        assert value is None

    def test_increasing_with_intensity(self) -> None:
        """Falsifiability: a histogram with higher intensity must produce strictly higher
        TRIMP (non-trivial function, not a constant)."""
        low = trimp_from_histogram({120: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        high = trimp_from_histogram({170: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        assert low is not None
        assert high is not None
        assert high > low

    def test_hrr_bounded_to_1_beyond_hr_max(self) -> None:
        at_max = trimp_from_histogram({190: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        above_max = trimp_from_histogram({210: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        assert at_max is not None
        assert above_max is not None
        assert math.isclose(at_max, above_max, rel_tol=1e-9)
