"""Tests for t0.compute.hr_histogram - 1 bpm resolution, no pre-binning into zones."""

from __future__ import annotations

from t0.compute.hr_histogram import hr_histogram


def test_hr_histogram_counts_seconds_per_bpm() -> None:
    hr = [140, 140, 141, 141, 141, 150]
    histogram = hr_histogram(hr)
    assert histogram == {140: 2, 141: 3, 150: 1}


def test_hr_histogram_skips_missing_samples() -> None:
    """A disconnected sensor (None) must never be counted as 0 bpm."""
    hr: list[int | None] = [140, None, None, 141]
    histogram = hr_histogram(hr)
    assert histogram == {140: 1, 141: 1}
    assert 0 not in histogram


def test_hr_histogram_skips_artifacts_outside_the_plausible_range() -> None:
    hr = [140, 5, 300, 141]
    histogram = hr_histogram(hr)
    assert histogram == {140: 1, 141: 1}


def test_hr_histogram_bounds_are_inclusive() -> None:
    hr = [20, 260]
    histogram = hr_histogram(hr)
    assert histogram == {20: 1, 260: 1}


def test_hr_histogram_empty_series_gives_empty_histogram() -> None:
    assert hr_histogram([]) == {}


def test_hr_histogram_is_falsifiable_not_a_plain_average() -> None:
    """Would fail if the implementation collapsed to a single aggregate (mean, min/max)."""
    hr_a = [140] * 10
    hr_b = [130] * 5 + [150] * 5
    assert hr_histogram(hr_a) != hr_histogram(hr_b)
    assert sum(hr_histogram(hr_a).values()) == sum(hr_histogram(hr_b).values()) == 10
