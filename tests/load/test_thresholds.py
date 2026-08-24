"""Resolve threshold in effect at date (core.threshold, 04 §2.3)."""

from __future__ import annotations

from datetime import date

import pytest

from garminator.load.thresholds import Threshold, pace_s_per_km_to_speed_mps, threshold_for_date


def _run_threshold(valid_from: date, valid_to: date | None, threshold_id: int) -> Threshold:
    return Threshold(
        discipline="run",
        valid_from=valid_from,
        valid_to=valid_to,
        threshold_id=threshold_id,
        ftp_pace_s_per_km=270.0,
    )


class TestThresholdForDate:
    def test_covers_date(self) -> None:
        thresholds = [_run_threshold(date(2026, 5, 19), None, threshold_id=1)]
        found = threshold_for_date(thresholds, "run", date(2026, 6, 1))
        assert found is not None
        assert found.threshold_id == 1

    def test_before_start_no_threshold(self) -> None:
        """Direct mirror of addendum §5: activities before 2026-05-19 legitimately have no
        threshold, no TSS—no exception, no fabricated threshold."""
        thresholds = [_run_threshold(date(2026, 5, 19), None, threshold_id=1)]
        found = threshold_for_date(thresholds, "run", date(2026, 1, 1))
        assert found is None

    def test_upper_bound_exclusive(self) -> None:
        thresholds = [_run_threshold(date(2026, 1, 1), date(2026, 6, 1), threshold_id=1)]
        assert threshold_for_date(thresholds, "run", date(2026, 5, 31)) is not None
        assert threshold_for_date(thresholds, "run", date(2026, 6, 1)) is None

    def test_different_discipline_ignored(self) -> None:
        thresholds = [_run_threshold(date(2026, 5, 19), None, threshold_id=1)]
        assert threshold_for_date(thresholds, "bike", date(2026, 6, 1)) is None

    def test_overlap_raises_error(self) -> None:
        """Should never happen in practice (GiST constraint threshold_no_overlap in DB),
        but if source data inconsistent, fail loudly rather than arbitrarily pick threshold."""
        thresholds = [
            _run_threshold(date(2026, 1, 1), None, threshold_id=1),
            _run_threshold(date(2026, 3, 1), None, threshold_id=2),
        ]
        with pytest.raises(ValueError, match="overlap"):
            threshold_for_date(thresholds, "run", date(2026, 6, 1))


class TestPaceConversion:
    def test_example_threshold_pace_4min30_per_km(self) -> None:
        speed = pace_s_per_km_to_speed_mps(270.0)
        assert speed is not None
        assert abs(speed - 3.7037037037037037) < 1e-9

    def test_missing_pace_returns_none(self) -> None:
        assert pace_s_per_km_to_speed_mps(None) is None

    def test_zero_pace_returns_none(self) -> None:
        """Pace of 0 s/km would yield infinite speed: refuse, not divide."""
        assert pace_s_per_km_to_speed_mps(0.0) is None

    def test_negative_pace_returns_none(self) -> None:
        assert pace_s_per_km_to_speed_mps(-270.0) is None
