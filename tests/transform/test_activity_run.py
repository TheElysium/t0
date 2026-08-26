"""Running with power AND heart rate, rFTP missing (athlete threshold reference:
rFTP run missing, never guessed). Verifies that run-power rTSS remains None without
ever inventing a threshold, and that canonical resolution falls back to run_gap (GAP)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from t0.transform.activity import build_core_rows

if TYPE_CHECKING:
    from t0.transform.rows import CoreActivityBundle

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    RUN_THRESHOLD,
    make_sample,
    parsed_source,
    run_context,
)

_DURATION_S = 600
_SPEED_MPS = 4.0  # 250 s/km, slower than threshold (4:30/km -> 3.70 m/s).
_ALTITUDE_M = 100.0  # flat: smoothed gradient = 0, never None -> gap_applied true.


def _run_bundle() -> CoreActivityBundle:
    samples = [
        make_sample(
            t,
            power=300,
            heart_rate=140 + (t // 60),
            speed_mps=_SPEED_MPS,
            distance_m=_SPEED_MPS * t,
            altitude_m=_ALTITUDE_M,
        )
        for t in range(_DURATION_S)
    ]
    decoded = parsed_source(samples)
    return build_core_rows(
        decoded,
        run_context(duration_s=_DURATION_S),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=RUN_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestRunWithMissingRftp:
    def test_run_power_tss_stays_none_without_rftp_never_guessed(self) -> None:
        bundle = _run_bundle()
        run_power_rows = [row for row in bundle.metrics if row.method_code == "run_power"]
        tss_row = next(row for row in run_power_rows if row.metric == "tss")
        assert tss_row.value is None
        assert tss_row.is_canonical is False
        # No intensity_factor row for a method whose IntensityResult is None.
        if_rows = [row for row in run_power_rows if row.metric == "intensity_factor"]
        assert if_rows == [] or if_rows[0].value is None

    def test_run_gap_becomes_canonical_method(self) -> None:
        bundle = _run_bundle()
        tss_run_gap = next(
            row for row in bundle.metrics if row.metric == "tss" and row.method_code == "run_gap"
        )
        assert tss_run_gap.value is not None
        assert tss_run_gap.is_canonical is True
        assert bundle.activity.needs_manual_rpe is False

    def test_gap_applied_because_smoothed_gradient_usable(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.ngp_speed_mps is not None
        assert bundle.activity.gap_applied is True

    def test_ngp_close_to_actual_speed_on_flat_ground(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.ngp_speed_mps == _SPEED_MPS

    def test_heart_rate_average_and_maximum_calculated(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.avg_hr is not None
        assert bundle.activity.max_hr is not None
        assert bundle.activity.max_hr >= bundle.activity.avg_hr

    def test_efficiency_factor_and_decoupling_use_method_code_run_gap(self) -> None:
        bundle = _run_bundle()
        ef_row = next(row for row in bundle.metrics if row.metric == "efficiency_factor")
        decoupling_row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        assert ef_row.method_code == "run_gap"
        assert decoupling_row.method_code == "run_gap"
        assert ef_row.threshold_id is None
        assert decoupling_row.threshold_id is None


class TestRunWithoutSpeedOrDistance:
    """Run with only heart rate (watch without GPS/cadence): no speed -> no GAP
    possible, never a value invented from nothing."""

    def test_ngp_none_and_gap_not_applied_without_speed(self) -> None:
        samples = [make_sample(t, heart_rate=140) for t in range(60)]
        decoded = parsed_source(samples)
        bundle = build_core_rows(
            decoded,
            run_context(duration_s=60),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=RUN_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.ngp_speed_mps is None
        assert bundle.activity.gap_applied is False
        # All running TSS methods depend on speed/power: none is calculable here,
        # only hr_hrtss could be (heart rate present).
        decoupling_rows = [row for row in bundle.metrics if row.metric == "decoupling_pct"]
        assert decoupling_rows == []
