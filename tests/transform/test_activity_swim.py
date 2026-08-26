"""Swimming: ``core.sample`` is empty by construction (see ``core.swim_length`` docstring
in migrations/004 -- all second-by-second data is in ``length`` messages, never in
GPS/sensor samples like other sports). Verifies that zero samples is never treated as
an error, and that swim time reconstruction correctly switches between two possible
sources (FIT lengths vs. total elapsed time)."""

from __future__ import annotations

from datetime import timedelta

from t0.fit.model import SwimLengthRow
from t0.transform.activity import build_core_rows

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    START,
    SWIM_THRESHOLD,
    make_sample,
    parsed_source,
    swim_context,
)


def _swim_lengths(count: int, duration_s: float) -> list[SwimLengthRow]:
    return [
        SwimLengthRow(
            activity_id=1,
            length_index=i,
            start_time=START + timedelta(seconds=i * duration_s),
            duration_s=duration_s,
            length_type="active",
        )
        for i in range(count)
    ]


class TestPoolSwimWithLengths:
    """Pool: ``length`` messages available -- most reliable time source."""

    def test_swim_time_reconstructed_from_active_lengths(self) -> None:
        lengths = _swim_lengths(40, 25.0)  # 40 x 25 m, 25 s/longueur -> 1000 s.
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_s == 1000.0
        assert bundle.activity.swim_time_source == "fit_lengths"
        assert bundle.activity.total_samples == 0
        assert bundle.samples == ()

    def test_data_quality_not_capped_to_fair_when_lengths_exist(self) -> None:
        lengths = _swim_lengths(4, 25.0)
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=200),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        # No power samples -> empty regularized channel -> quality "unknown",
        # never forced to "fair" (this cap applies ONLY to the elapsed-time fallback).
        assert bundle.activity.data_quality == "unknown"


class TestOpenWaterSwimWithoutLengths:
    """Open water: no ``length`` messages -- fallback to elapsed time, less reliable."""

    def test_swim_time_falls_back_to_elapsed_time(self) -> None:
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_s == 1800.0
        assert bundle.activity.swim_time_source == "elapsed"

    def test_data_quality_capped_to_fair_when_power_channel_is_good(self) -> None:
        # Rare but possible (connected watch with power estimator while swimming):
        # power channel alone would be "good", but elapsed-time fallback for swim time
        # remains an approximation -- the §4.3 cap applies anyway, with or without power.
        samples = [make_sample(t, power=100) for t in range(60)]
        decoded = parsed_source(samples, swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800, activity_id=1),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_source == "elapsed"
        assert bundle.activity.data_quality == "fair"

    def test_idle_lengths_ignored_in_reconstruction(self) -> None:
        lengths = [
            SwimLengthRow(
                activity_id=1,
                length_index=0,
                start_time=START,
                duration_s=10.0,
                length_type="idle",  # turn/rest: never counted as active swimming.
            )
        ]
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        # No "active" lengths -> elapsed-time fallback, not 10 s of idle length.
        assert bundle.activity.swim_time_source == "elapsed"
        assert bundle.activity.swim_time_s == 1800.0

    def test_zero_samples_not_error_empty_histogram_and_curve(self) -> None:
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.hr_histogram == ()
        assert bundle.power_curve == ()
        assert bundle.laps == ()

    def test_decoupling_out_of_scope_for_swimming(self) -> None:
        # Explicit descope (see _load_extra_metric_rows docstring): no decoupling
        # formula is specified for swimming in this batch.
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert [row for row in bundle.metrics if row.metric == "decoupling_pct"] == []
        assert [row for row in bundle.metrics if row.metric == "efficiency_factor"] == []
