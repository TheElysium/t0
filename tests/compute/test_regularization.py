"""Tests de garminator.compute.regularization : trous, interpolation, excision de pauses.

Pas de cas numerique auto-portant dans 04-formules-charge.md pour cette brique (elle
est un prerequis mecanique aux algorithmes du §1.3, pas une formule a elle seule) ;
les tests ci-dessous transcrivent le comportement impose en toutes lettres par la
specification et sont concus pour echouer si le comportement est absent ou incorrect.
"""

from __future__ import annotations

import math

import pytest

from garminator.compute.regularization import Gap, detect_gaps, regularize_series


def test_detect_gaps_aucun_trou_sur_serie_continue() -> None:
    assert detect_gaps([0, 1, 2, 3, 4]) == []


def test_detect_gaps_repere_un_trou_unique() -> None:
    gaps = detect_gaps([0, 1, 2, 7, 8])
    assert gaps == [Gap(start_s=3, end_s=6)]
    assert gaps[0].length_s == 4


def test_detect_gaps_plusieurs_trous() -> None:
    gaps = detect_gaps([0, 5, 6, 20])
    assert gaps == [Gap(start_s=1, end_s=4), Gap(start_s=7, end_s=19)]


def test_detect_gaps_leve_value_error_si_pas_strictement_croissant() -> None:
    """Un doublon ou un instant qui recule est une erreur de programmation, pas un trou."""
    with pytest.raises(ValueError, match="croissant"):
        detect_gaps([0, 1, 1, 2])
    with pytest.raises(ValueError, match="croissant"):
        detect_gaps([0, 2, 1])


def test_regularize_series_trou_court_est_interpole_lineairement() -> None:
    """Trou de 2 s (<=3 s) entre 100 et 200 -> deux points intermediaires interpoles."""
    result = regularize_series([0, 3], [100.0, 200.0])
    assert result.total_seconds == 4
    expected = (100.0, 133.33333333333334, 166.66666666666666, 200.0)
    assert result.values == pytest.approx(expected)
    assert result.imputed == (False, True, True, False)
    assert result.imputed_seconds == 2
    assert result.excised_seconds == 0


def test_regularize_series_trou_long_hors_pause_est_impute_a_zero() -> None:
    """Trou de 5 s (>3 s) sans intervalle de pause fourni -> imputation conservatrice a 0."""
    result = regularize_series([0, 5], [200.0, 300.0])
    assert result.total_seconds == 6
    assert result.values == (200.0, 0.0, 0.0, 0.0, 0.0, 300.0)
    assert result.imputed == (False, True, True, True, True, False)
    assert result.imputed_seconds == 4
    assert result.excised_seconds == 0


def test_regularize_series_trou_long_dans_une_pause_est_excise() -> None:
    """Trou de 5 s entierement couvert par une pause -> excise, pas de secondes creees."""
    result = regularize_series([0, 5], [200.0, 300.0], pause_intervals=[(1, 4)])
    assert result.total_seconds == 2
    assert result.values == (200.0, 300.0)
    assert result.imputed == (False, False)
    assert result.imputed_seconds == 0
    assert result.excised_seconds == 4


def test_regularize_series_pause_partielle_n_est_pas_excisee() -> None:
    """Intervalle de pause ne couvrant pas tout le trou -> pas d'excision, imputation normale."""
    result = regularize_series([0, 5], [200.0, 300.0], pause_intervals=[(1, 2)])
    assert result.excised_seconds == 0
    assert result.imputed_seconds == 4


def test_regularize_series_qualite_poor_au_dela_de_10_pourcent_impute() -> None:
    # 20 s de serie, 3 s imputees (trou de 3 s > seuil 10% -> poor).
    result = regularize_series([0, 4, 24], [100.0, 100.0, 100.0])
    assert result.imputed_seconds / result.total_seconds > 0.10
    assert result.data_quality == "poor"


def test_regularize_series_qualite_good_sous_10_pourcent_impute() -> None:
    result = regularize_series([0, *range(2, 101)], [100.0] * 100)
    assert result.imputed_seconds / result.total_seconds <= 0.10
    assert result.data_quality == "good"


def test_regularize_series_vide_donne_quality_unknown() -> None:
    result = regularize_series([], [])
    assert result.total_seconds == 0
    assert result.data_quality == "unknown"
    assert result.values == ()


def test_regularize_series_longueurs_incoherentes_leve_value_error() -> None:
    with pytest.raises(ValueError, match="longueur"):
        regularize_series([0, 1], [100.0])


def test_regularize_series_expose_les_indices_de_raccord_pour_la_courbe_puissance() -> None:
    """Sans ce champ, best_effort_spans_pause n'a aucune source pour ses raccords.

    Trou de 10 s entierement couvert par une pause -> excision : l'echantillon qui suit
    est le premier d'un nouveau segment, non contigu en temps reel avec le precedent.
    """
    result = regularize_series(
        [0, 1, 2, 13, 14], [10.0, 11.0, 12.0, 13.0, 14.0], pause_intervals=[(3, 12)]
    )
    assert result.excised_seconds == 10
    assert result.values == (10.0, 11.0, 12.0, 13.0, 14.0)
    assert result.segment_boundaries == (3,)
    # Le raccord pointe bien sur le premier echantillon d'apres la pause.
    assert result.values[result.segment_boundaries[0]] == 13.0


def test_regularize_series_plusieurs_excisions_donnent_plusieurs_raccords() -> None:
    result = regularize_series(
        [0, 10, 11, 21],
        [1.0, 2.0, 3.0, 4.0],
        pause_intervals=[(1, 9), (12, 20)],
    )
    assert result.segment_boundaries == (1, 3)
    assert result.excised_seconds == 18
    assert result.total_seconds == 4


def test_regularize_series_sans_pause_aucun_raccord() -> None:
    assert regularize_series([0, 1, 2], [1.0, 2.0, 3.0]).segment_boundaries == ()
    # Un trou impute (et non excise) n'est PAS un raccord : la serie reste contigue.
    assert regularize_series([0, 5], [1.0, 2.0]).segment_boundaries == ()


def test_regularize_series_trou_de_3s_est_la_borne_haute_interpolee() -> None:
    """max_interp_gap_s=3 : un trou de 3 s est interpole, un trou de 4 s ne l'est plus."""
    trois = regularize_series([0, 4], [0.0, 4.0])
    assert trois.values == (0.0, 1.0, 2.0, 3.0, 4.0)
    quatre = regularize_series([0, 5], [0.0, 5.0])
    assert quatre.values == (0.0, 0.0, 0.0, 0.0, 0.0, 5.0)


def test_regularize_series_long_trou_n_est_jamais_interpole() -> None:
    """Un trou de 60 s doit etre rempli a fill_value, jamais interpole en rampe."""
    result = regularize_series([0, 61], [100.0, 400.0])
    assert result.total_seconds == 62
    assert set(result.values[1:61]) == {0.0}
    assert result.imputed_seconds == 60
    assert result.data_quality == "poor"


def test_regularize_series_fill_value_personnalise() -> None:
    """fill_value n'est pas cable a 0 : une serie de FC ne s'impute pas a 0 bpm."""
    result = regularize_series([0, 5], [140.0, 150.0], fill_value=math.nan)
    assert all(math.isnan(v) for v in result.values[1:5])
    assert result.values[0] == 140.0
    assert result.values[5] == 150.0


def test_regularize_series_un_seul_echantillon() -> None:
    result = regularize_series([7], [200.0])
    assert result.values == (200.0,)
    assert result.total_seconds == 1
    assert result.data_quality == "good"
    assert result.segment_boundaries == ()


def test_regularize_series_pause_declaree_sur_un_trou_court_ne_prime_pas_sur_l_interpolation() -> (
    None
):
    """§1.3 : l'ordre est impose — <=3 s interpole AVANT tout test de pause."""
    result = regularize_series([0, 3], [100.0, 200.0], pause_intervals=[(1, 2)])
    assert result.excised_seconds == 0
    assert result.total_seconds == 4


def test_regularize_series_temps_non_croissant_leve_value_error() -> None:
    """Un instant qui recule ou se repete est une erreur d'appelant, jamais un trou.

    Sans ce garde-fou, gap = curr_t - prev_t - 1 deviendrait negatif et la boucle
    d'interpolation serait silencieusement sautee : la serie sortirait plus courte que
    prevu, sans aucun signal.
    """
    with pytest.raises(ValueError, match="croissant"):
        regularize_series([0, 5, 5], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="croissant"):
        regularize_series([0, 5, 3], [1.0, 2.0, 3.0])
