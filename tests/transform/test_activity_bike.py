"""Vélo home-trainer sans ceinture cardio (cf. profil-athlete.md : puissance vélo
uniquement en home-trainer, pas toujours de ceinture portée). Vérifie que l'absence de
FC ne fait dériver aucun calcul vers un 0 fabriqué et que le TSS reste calculable par
puissance seule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.transform.activity import build_core_rows

if TYPE_CHECKING:
    from garminator.transform.rows import CoreActivityBundle

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    bike_context,
    make_sample,
    parsed_source,
)

_DURATION_S = 600
_POWER_W = 176.0


def _bike_bundle() -> CoreActivityBundle:
    samples = [make_sample(t, power=int(_POWER_W)) for t in range(_DURATION_S)]
    decoded = parsed_source(samples)
    return build_core_rows(
        decoded,
        bike_context(duration_s=_DURATION_S),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestVeloHomeTrainerSansCardio:
    """Puissance constante 176 W pendant 10 min, FTP = 220 W (cf. seuils-athlete.md)."""

    def test_puissance_constante_donne_np_egale_a_la_puissance_moyenne(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.avg_power == _POWER_W
        assert bundle.activity.np_watts == _POWER_W

    def test_fc_absente_reste_none_jamais_zero(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.avg_hr is None
        assert bundle.activity.max_hr is None

    def test_tss_puissance_seule_rend_needs_manual_rpe_faux(self) -> None:
        bundle = _bike_bundle()
        tss_rows = {row.method_code: row for row in bundle.metrics if row.metric == "tss"}
        assert "bike_power" in tss_rows
        assert tss_rows["bike_power"].is_canonical
        assert tss_rows["bike_power"].value is not None
        assert bundle.activity.needs_manual_rpe is False

    def test_intensity_factor_reutilise_le_meme_method_code_que_le_tss_canonique(self) -> None:
        bundle = _bike_bundle()
        tss_canonical = next(
            row for row in bundle.metrics if row.metric == "tss" and row.is_canonical
        )
        if_rows = {
            row.method_code: row for row in bundle.metrics if row.metric == "intensity_factor"
        }
        assert tss_canonical.method_code in if_rows
        assert if_rows[tss_canonical.method_code].is_canonical is tss_canonical.is_canonical
        # IF = NP / FTP = 176 / 220 = 0.8, exactement.
        assert if_rows[tss_canonical.method_code].value == 0.8

    def test_efficiency_factor_et_decoupling_none_sans_fc_jamais_zero(self) -> None:
        bundle = _bike_bundle()
        ef_rows = [row for row in bundle.metrics if row.metric == "efficiency_factor"]
        decoupling_rows = [row for row in bundle.metrics if row.metric == "decoupling_pct"]
        assert len(ef_rows) == 1
        assert len(decoupling_rows) == 1
        assert ef_rows[0].value is None
        assert ef_rows[0].is_canonical is False
        assert decoupling_rows[0].value is None

    def test_trimp_none_sans_histogramme_fc(self) -> None:
        bundle = _bike_bundle()
        trimp_row = next(row for row in bundle.metrics if row.metric == "trimp")
        assert trimp_row.value is None
        assert trimp_row.is_canonical is False

    def test_np_reliable_vrai_au_dela_du_seuil_de_fiabilite(self) -> None:
        # 600 s >= RELIABLE_THRESHOLD_S (300 s, cf. garminator.compute.normalized_power).
        bundle = _bike_bundle()
        assert bundle.activity.np_reliable is True

    def test_total_samples_egale_le_nombre_brut_d_echantillons(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.total_samples == _DURATION_S

    def test_aucune_pente_ni_ngp_en_velo(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.ngp_speed_mps is None
        assert bundle.activity.gap_applied is False
