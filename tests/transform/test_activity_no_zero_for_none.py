"""Test de non-régression transversal : sur une activité entièrement dépourvue de
données exploitables (aucun échantillon), AUCUN champ "non calculable" de
``ActivityRow`` ne doit valoir 0 -- seul ``None`` représente une grandeur non
calculable dans ce schéma (cf. docstring de ``garminator.transform.rows`` et le
commentaire de ``core.activity_metric.value`` dans migrations/005_core_series.sql).

Ce test balaie explicitement les champs *nullable* de la DDL -- il exclut à dessein les
compteurs qui ont légitimement 0 comme valeur par défaut significative
(``imputed_seconds``/``excised_seconds``/``gap_applied``/``is_parent``/
``needs_manual_rpe``), qui ne sont PAS des grandeurs "non calculable" mais des
compteurs/booléens dont 0/False est la valeur réelle en l'absence d'anomalie."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.transform.activity import build_core_rows

if TYPE_CHECKING:
    from garminator.transform.rows import CoreActivityBundle

from .conftest import ALL_THRESHOLD, ATHLETE_ID, BIKE_THRESHOLD, bike_context, parsed_source

#: Champs nullables de core.activity (migrations/004) qui représentent une grandeur
#: "non calculable" -- jamais un compteur ou un flag booléen à défaut significatif.
_NULLABLE_METRIC_FIELDS = (
    "distance_m",
    "ascent_m",
    "descent_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "max_power",
    "avg_power_nonzero",
    "np_watts",
    "np_reliable",
    "avg_speed_mps",
    "avg_cadence",
    "calories",
    "ngp_speed_mps",
    "swim_time_s",
    "swim_time_source",
)


def _empty_bike_bundle() -> CoreActivityBundle:
    decoded = parsed_source([])
    return build_core_rows(
        decoded,
        bike_context(duration_s=3600),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestAucunZeroSubstitueANoneSurActiviteVide:
    def test_tous_les_champs_nullable_de_activity_row_sont_none(self) -> None:
        bundle = _empty_bike_bundle()
        for field_name in _NULLABLE_METRIC_FIELDS:
            value = getattr(bundle.activity, field_name)
            assert value is None, f"{field_name} vaut {value!r}, attendu None"

    def test_les_compteurs_legitimes_restent_a_leur_valeur_par_defaut_zero(self) -> None:
        bundle = _empty_bike_bundle()
        assert bundle.activity.imputed_seconds == 0
        assert bundle.activity.excised_seconds == 0
        assert bundle.activity.total_samples == 0

    def test_aucune_ligne_activity_metric_ne_vaut_zero_toutes_valent_none(self) -> None:
        bundle = _empty_bike_bundle()
        for row in bundle.metrics:
            assert row.value is None, f"{row.metric}/{row.method_code} vaut {row.value!r}"
            assert row.is_canonical is False

    def test_needs_manual_rpe_vrai_car_aucune_methode_calculee_n_a_produit_de_tss(self) -> None:
        bundle = _empty_bike_bundle()
        assert bundle.activity.needs_manual_rpe is True
