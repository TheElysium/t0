"""End-to-end acceptance tests for L5 batch (activity_load.py orchestration).

Each test here verifies an explicit acceptance criterion from the mandate: (1) a road bike
without power sensor falls back to hrTSS and logs it, (2) TRIMP is None and never 0
without HR, (3) an activity before the threshold date (2026-05-19 for this athlete) has
neither fabricated TSS nor exception."""

from __future__ import annotations

from datetime import date

from garminator.load.activity_load import (
    ActivityLoadInput,
    build_activity_metric_rows,
    needs_manual_rpe,
)
from garminator.load.thresholds import Threshold

_ALL_THRESHOLD = Threshold(
    discipline="all",
    valid_from=date(2026, 5, 19),
    valid_to=None,
    threshold_id=100,
    lthr_bpm=170.0,
    hr_max_bpm=190.0,
    hr_rest_bpm=55.0,
)


def _hr_histogram_around(bpm: int, seconds: int) -> dict[int, int]:
    return {bpm: seconds}


class TestRoadBikeFallsBackToHrTss:
    def test_road_bike_without_power_falls_back_to_hr_hrtss_and_logs_it(self) -> None:
        bike_threshold = Threshold(
            discipline="bike",
            valid_from=date(2026, 5, 19),
            valid_to=None,
            threshold_id=1,
            ftp_watts=220.0,
        )
        data = ActivityLoadInput(
            activity_id=42,
            sport_code="cycling",
            discipline="bike",
            duration_s=3600.0,
            distance_m=30000.0,
            swim_time_s=None,
            np_watts=None,  # no power sensor on road bike
            ngp_speed_mps=None,
            hr_histogram=_hr_histogram_around(180, 3600),
            threshold=bike_threshold,
            all_threshold=_ALL_THRESHOLD,
        )
        rows = build_activity_metric_rows(data)
        tss_rows = {r.method_code: r for r in rows if r.metric == "tss"}

        assert tss_rows["bike_power"].value is None
        assert tss_rows["bike_power"].is_canonical is False
        assert tss_rows["hr_hrtss"].value is not None
        assert tss_rows["hr_hrtss"].is_canonical is True
        assert tss_rows["hr_hrtss"].threshold_id == _ALL_THRESHOLD.threshold_id
        assert sum(r.is_canonical for r in tss_rows.values()) == 1
        assert needs_manual_rpe(rows) is False

    def test_indoor_bike_with_power_only_uses_bike_power_and_masks_trimp(self) -> None:
        """Indoor bike (TCX): power present, HR absent. TSS is computed from bike_power;
        TRIMP stays None, never 0."""
        bike_threshold = Threshold(
            discipline="bike",
            valid_from=date(2026, 5, 19),
            valid_to=None,
            threshold_id=1,
            ftp_watts=220.0,
        )
        data = ActivityLoadInput(
            activity_id=43,
            sport_code="virtual_cycling",
            discipline="bike",
            duration_s=3600.0,
            distance_m=None,
            swim_time_s=None,
            np_watts=230.0,
            ngp_speed_mps=None,
            hr_histogram={},  # TCX indoor: no HR
            threshold=bike_threshold,
            all_threshold=_ALL_THRESHOLD,
        )
        rows = build_activity_metric_rows(data)
        tss_rows = {r.method_code: r for r in rows if r.metric == "tss"}
        trimp_row = next(r for r in rows if r.metric == "trimp")

        assert tss_rows["bike_power"].is_canonical is True
        assert tss_rows["bike_power"].value is not None
        assert trimp_row.value is None
        assert trimp_row.is_canonical is False


class TestActivityBeforeThresholdValidity:
    def test_no_threshold_before_2026_05_19_no_fabricated_tss_no_exception(self) -> None:
        data = ActivityLoadInput(
            activity_id=7,
            sport_code="running",
            discipline="run",
            duration_s=2700.0,
            distance_m=6000.0,
            swim_time_s=None,
            np_watts=None,
            ngp_speed_mps=3.5,
            hr_histogram=_hr_histogram_around(150, 2700),
            threshold=None,  # activity before backdate date
            all_threshold=None,
        )
        rows = build_activity_metric_rows(data)
        tss_rows = [r for r in rows if r.metric == "tss"]
        assert all(r.value is None for r in tss_rows)
        assert all(r.is_canonical is False for r in tss_rows)
        assert needs_manual_rpe(rows) is True
        trimp_row = next(r for r in rows if r.metric == "trimp")
        assert trimp_row.value is None


class TestRunFallsBackFromMissingRftp:
    def test_run_with_gap_available_rftp_null(self) -> None:
        run_threshold = Threshold(
            discipline="run",
            valid_from=date(2026, 5, 19),
            valid_to=None,
            threshold_id=2,
            ftp_pace_s_per_km=270.0,
            rftp_watts=None,  # never guessed, see non-negotiable business rule
        )
        data = ActivityLoadInput(
            activity_id=8,
            sport_code="running",
            discipline="run",
            duration_s=2700.0,
            distance_m=6000.0,
            swim_time_s=None,
            np_watts=None,
            ngp_speed_mps=3.7037037037037037,
            hr_histogram=_hr_histogram_around(160, 2700),
            threshold=run_threshold,
            all_threshold=_ALL_THRESHOLD,
        )
        rows = build_activity_metric_rows(data)
        tss_rows = {r.method_code: r for r in rows if r.metric == "tss"}
        assert tss_rows["run_power"].value is None
        assert tss_rows["run_power"].is_canonical is False
        assert tss_rows["run_gap"].is_canonical is True
        assert tss_rows["run_gap"].value is not None


class TestSwim:
    def test_swim_uses_swim_css_on_active_time(self) -> None:
        """Addendum §4: swimming lives in ``length`` messages, so ``swim_time_s``
        (active time) not ``duration_s`` (which includes rests)."""
        swim_threshold = Threshold(
            discipline="swim",
            valid_from=date(2026, 5, 19),
            valid_to=None,
            threshold_id=3,
            css_m_s=0.909091,
        )
        data = ActivityLoadInput(
            activity_id=9,
            sport_code="lap_swimming",
            discipline="swim",
            duration_s=3600.0,  # with rests
            distance_m=1800.0,
            swim_time_s=1800.0,  # active time only
            np_watts=None,
            ngp_speed_mps=None,
            hr_histogram={},  # no HR in pool
            threshold=swim_threshold,
            all_threshold=_ALL_THRESHOLD,
        )
        rows = build_activity_metric_rows(data)
        tss_rows = {r.method_code: r for r in rows if r.metric == "tss"}
        assert tss_rows["swim_css"].is_canonical is True
        assert tss_rows["swim_css"].value is not None
        assert tss_rows["swim_css"].threshold_id == swim_threshold.threshold_id
        assert tss_rows["hr_hrtss"].value is None
        assert needs_manual_rpe(rows) is False
        # 1800 m in 1800 s = 1.0 m/s against the example CSS (0.909091 m/s) -> IF = 1.1,
        # over 0.5 h -> sTSS = 0.5 * 1.1**3 * 100 = 66.55.
        assert abs(tss_rows["swim_css"].value - 66.55) < 1e-3

    def test_swim_without_css_invents_no_tss(self) -> None:
        data = ActivityLoadInput(
            activity_id=10,
            sport_code="open_water",
            discipline="swim",
            duration_s=3600.0,
            distance_m=1800.0,
            swim_time_s=1800.0,
            np_watts=None,
            ngp_speed_mps=None,
            hr_histogram={},
            threshold=None,
            all_threshold=_ALL_THRESHOLD,
        )
        rows = build_activity_metric_rows(data)
        assert all(r.value is None for r in rows if r.metric == "tss")
        assert needs_manual_rpe(rows) is True
