"""Tests d'acceptance de bout en bout du lot L5 (orchestration activity_load.py).

Chaque test ici vérifie un critère d'acceptance explicite du mandat : (1) un vélo
route sans capteur de puissance retombe sur hrTSS et le trace, (2) TRIMP est None et
jamais 0 sans FC, (3) une activité antérieure au seuil (2026-05-19 pour cet athlète)
n'a ni TSS fabriqué ni exception."""

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
    def test_velo_route_sans_puissance_retombe_sur_hr_hrtss_et_le_trace(self) -> None:
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
            np_watts=None,  # pas de capteur de puissance sur le vélo route
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

    def test_velo_indoor_avec_puissance_seule_utilise_bike_power_et_masque_trimp(self) -> None:
        """Vélo indoor (TCX) : puissance présente, FC absente (addendum §2-3). TSS doit
        être calculé normalement via bike_power ; TRIMP doit rester None, jamais 0."""
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
            hr_histogram={},  # TCX indoor : pas de FC
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
    def test_aucun_seuil_avant_2026_05_19_pas_de_tss_fabrique_pas_dexception(self) -> None:
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
            threshold=None,  # activité antérieure à la date de backdating
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
    def test_course_avec_gap_disponible_rftp_null(self) -> None:
        run_threshold = Threshold(
            discipline="run",
            valid_from=date(2026, 5, 19),
            valid_to=None,
            threshold_id=2,
            ftp_pace_s_per_km=270.0,
            rftp_watts=None,  # jamais deviné, cf. règle métier non négociable
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
    def test_natation_utilise_swim_css_sur_le_temps_actif(self) -> None:
        """Addendum §4 : la natation vit dans les messages ``length``, donc
        ``swim_time_s`` (temps actif) et non ``duration_s`` (qui inclut les repos)."""
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
            duration_s=3600.0,  # avec les repos
            distance_m=1800.0,
            swim_time_s=1800.0,  # temps actif seul
            np_watts=None,
            ngp_speed_mps=None,
            hr_histogram={},  # pas de FC en piscine
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
        # 1800 m en 1800 s = 1.0 m/s face au CSS d'exemple (0.909091 m/s) -> IF = 1.1,
        # sur 0.5 h -> sTSS = 0.5 * 1.1**3 * 100 = 66.55.
        assert abs(tss_rows["swim_css"].value - 66.55) < 1e-3

    def test_natation_sans_css_ne_fabrique_pas_de_tss(self) -> None:
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
