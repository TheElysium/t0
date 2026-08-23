"""Trou de capteur de puissance : vérifie ``imputed_seconds``/``excised_seconds``/
``data_quality`` (dérivés du seul canal puissance, cf. docstring de
``core.activity.imputed_seconds`` dans migrations/004) et que les moyennes brutes
(``avg_power``/``avg_power_nonzero``/``max_power``) restent calculées sur la série NON
imputée, pour ne pas polluer le diagnostic roue-libre-vs-perte-capteur."""

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

_GAP_S = 40  # > max_interp_gap_s (3 s) -> imputation à 0, jamais une interpolation.


def _bundle_with_gap() -> CoreActivityBundle:
    before = [make_sample(t, power=200) for t in range(10)]
    after = [make_sample(t, power=200) for t in range(10 + _GAP_S, 10 + _GAP_S + 10)]
    samples = before + after
    decoded = parsed_source(samples)
    return build_core_rows(
        decoded,
        bike_context(duration_s=10 + _GAP_S + 10),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestTrouDeCapteurPuissance:
    def test_imputed_seconds_reflete_exactement_le_trou(self) -> None:
        bundle = _bundle_with_gap()
        assert bundle.activity.imputed_seconds == _GAP_S
        assert bundle.activity.excised_seconds == 0  # aucune pause déclarée par l'appelant.

    def test_data_quality_degradee_a_poor_au_dela_de_10_pour_cent_impute(self) -> None:
        bundle = _bundle_with_gap()
        # 40 s imputées sur 60 s totales (10 + 40 + 10) = 66 % > seuil de 10 %.
        assert bundle.activity.data_quality == "poor"

    def test_moyennes_brutes_ignorent_les_secondes_imputees(self) -> None:
        bundle = _bundle_with_gap()
        # Les 20 échantillons réels valent tous 200 W : la moyenne brute ne doit PAS
        # être tirée vers le bas par les 40 s à 0 W injectées pour la régularisation.
        assert bundle.activity.avg_power == 200.0
        assert bundle.activity.avg_power_nonzero == 200.0
        assert bundle.activity.max_power == 200.0

    def test_np_watts_calcule_sur_la_serie_regularisee_donc_impactee_par_le_trou(self) -> None:
        bundle = _bundle_with_gap()
        # Contraste volontaire avec le test précédent : NP, lui, DOIT refléter le trou
        # (c'est le canal régularisé qui alimente NP, par construction du lot L4).
        assert bundle.activity.np_watts is not None
        assert bundle.activity.np_watts < 200.0

    def test_np_reliable_faux_sous_le_seuil_de_fiabilite_mais_pas_none(self) -> None:
        bundle = _bundle_with_gap()
        # 60 s de série régularisée : >= window_s (30) mais < RELIABLE_THRESHOLD_S (300).
        assert bundle.activity.np_reliable is False
