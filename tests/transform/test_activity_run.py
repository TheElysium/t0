"""Course à pied avec puissance ET cardio, rFTP manquant (cf. seuils-athlete.md :
« rFTP course manquant, jamais deviné »). Vérifie que le rTSS-puissance reste None sans
jamais inventer de seuil, et que la résolution canonique retombe sur run_gap (GAP)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.transform.activity import build_core_rows

if TYPE_CHECKING:
    from garminator.transform.rows import CoreActivityBundle

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    RUN_THRESHOLD,
    make_sample,
    parsed_source,
    run_context,
)

_DURATION_S = 600
_SPEED_MPS = 4.0  # 250 s/km, plus lent que le seuil (4:30/km -> 3.70 m/s).
_ALTITUDE_M = 100.0  # plat : pente lissée = 0, jamais None -> gap_applied vrai.


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


class TestCourseAvecRftpManquant:
    def test_run_power_tss_reste_none_sans_rftp_jamais_devine(self) -> None:
        bundle = _run_bundle()
        run_power_rows = [row for row in bundle.metrics if row.method_code == "run_power"]
        tss_row = next(row for row in run_power_rows if row.metric == "tss")
        assert tss_row.value is None
        assert tss_row.is_canonical is False
        # Pas de ligne intensity_factor pour une méthode dont l'IntensityResult est None.
        if_rows = [row for row in run_power_rows if row.metric == "intensity_factor"]
        assert if_rows == [] or if_rows[0].value is None

    def test_run_gap_devient_la_methode_canonique(self) -> None:
        bundle = _run_bundle()
        tss_run_gap = next(
            row for row in bundle.metrics if row.metric == "tss" and row.method_code == "run_gap"
        )
        assert tss_run_gap.value is not None
        assert tss_run_gap.is_canonical is True
        assert bundle.activity.needs_manual_rpe is False

    def test_gap_applique_car_pente_lissee_exploitable(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.ngp_speed_mps is not None
        assert bundle.activity.gap_applied is True

    def test_ngp_proche_de_la_vitesse_reelle_sur_terrain_plat(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.ngp_speed_mps == _SPEED_MPS

    def test_cardio_moyenne_et_maximum_calcules(self) -> None:
        bundle = _run_bundle()
        assert bundle.activity.avg_hr is not None
        assert bundle.activity.max_hr is not None
        assert bundle.activity.max_hr >= bundle.activity.avg_hr

    def test_efficiency_factor_et_decoupling_utilisent_le_method_code_run_gap(self) -> None:
        bundle = _run_bundle()
        ef_row = next(row for row in bundle.metrics if row.metric == "efficiency_factor")
        decoupling_row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        assert ef_row.method_code == "run_gap"
        assert decoupling_row.method_code == "run_gap"
        assert ef_row.threshold_id is None
        assert decoupling_row.threshold_id is None


class TestCourseSansVitesseNiDistance:
    """Footing avec seulement la FC (montre sans GPS/foulée) : aucune vitesse -> pas
    de GAP possible, jamais une valeur inventée à partir de rien."""

    def test_ngp_none_et_gap_non_applique_sans_vitesse(self) -> None:
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
        # Toutes les méthodes de TSS course dépendent de la vitesse/puissance :
        # aucune n'est calculable ici, seul hr_hrtss pourrait l'être (FC présente).
        decoupling_rows = [row for row in bundle.metrics if row.metric == "decoupling_pct"]
        assert decoupling_rows == []
