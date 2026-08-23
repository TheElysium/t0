"""Tests de garminator.compute.power_curve — cas PC-1 et PC-2 de 04-formules-charge.md §8."""

from __future__ import annotations

from itertools import pairwise

import pytest

from garminator.compute.power_curve import (
    best_effort,
    best_effort_spans_pause,
    log_spaced_durations,
    power_duration_curve,
)

# PC-1 : 30 echantillons, plateaux 10x100W puis 5x400W puis 15x150W (§8.5 de la spec).
_PC1_SERIES = [100.0] * 10 + [400.0] * 5 + [150.0] * 15


def test_pc1_best_5s_sur_le_plateau_haut() -> None:
    assert best_effort(_PC1_SERIES, 5) == 400.0


def test_pc1_best_10s_chevauche_le_plateau_haut_et_bas() -> None:
    """best(10) = 275.0, obtenu sur la fenetre 10..19 (5 s a 400 W + 5 s a 150 W).

    Verification par force brute ci-dessous : la prose de la spec §8.5 designait la
    fenetre « 5..14 » (5x100 + 5x400 = 2500, /10 = 250.0), qui n'est PAS la meilleure.
    La valeur 275.0 est correcte, la prose etait fausse ; elle a ete corrigee dans la
    spec. Ce test verrouille a la fois la valeur ET l'emplacement de la fenetre, pour
    qu'une implementation qui trouverait 275.0 par accident echoue.
    """
    assert best_effort(_PC1_SERIES, 10) == 275.0
    assert sum(_PC1_SERIES[5:15]) / 10 == 250.0
    assert sum(_PC1_SERIES[10:20]) / 10 == 275.0
    brute = max(sum(_PC1_SERIES[i : i + 10]) / 10 for i in range(len(_PC1_SERIES) - 10 + 1))
    assert brute == 275.0


def test_pc1_best_30s_egale_la_moyenne_globale() -> None:
    assert best_effort(_PC1_SERIES, 30) == 175.0


def test_pc1_best_31s_hors_de_portee_est_none() -> None:
    """Duree plus longue que la serie -> None, jamais une valeur tronquee ou 0."""
    assert best_effort(_PC1_SERIES, 31) is None


def test_pc2_monotonicite_intra_activite() -> None:
    """PC-2 : pour une meme activite, best(d1) >= best(d2) quand d1 < d2 (propriete generale)."""
    series = [100.0, 200.0, 300.0, 150.0, 250.0, 50.0, 400.0, 100.0, 300.0, 200.0] * 5
    durations = [1, 2, 5, 10, 20, 30, 50]
    values = [best_effort(series, d) for d in durations]
    for shorter, longer in pairwise(values):
        assert shorter is not None
        assert longer is not None
        assert shorter >= longer


def test_best_effort_duree_nulle_ou_negative_est_none() -> None:
    assert best_effort([1.0, 2.0, 3.0], 0) is None
    assert best_effort([1.0, 2.0, 3.0], -1) is None


def test_best_effort_serie_vide_est_none() -> None:
    assert best_effort([], 1) is None


def test_log_spaced_durations_croissante_et_dedupliquee() -> None:
    durations = log_spaced_durations(1, 3600)
    assert durations == sorted(set(durations))
    assert durations[0] == 1
    assert durations[-1] <= 3600
    # Densite attendue par la spec : de l'ordre de quelques dizaines a une centaine
    # de points pour 3600 s de plage (§8.2).
    assert 20 <= len(durations) <= 120


def test_log_spaced_durations_respecte_les_bornes() -> None:
    durations = log_spaced_durations(5, 100)
    assert all(5 <= d <= 100 for d in durations)


def test_power_duration_curve_durees_hors_de_portee_sont_none() -> None:
    series = [100.0] * 20
    curve = power_duration_curve(series, min_s=1, max_s=50)
    # Aucune duree generee au-dela de la longueur de la serie n'a de sens ; celles qui
    # le sont malgre tout (max_s force plus grand que la serie) doivent rester None.
    assert any(d > 20 for d in curve)
    for d, value in curve.items():
        if d > 20:
            assert value is None
        else:
            assert value is not None


def test_best_effort_spans_pause_signale_une_fenetre_qui_chevauche_une_excision() -> None:
    """Une fenetre gagnante qui traverse un indice d'excision est marquee spans_pause=True.

    La serie est volontairement NON constante : la meilleure fenetre de 4 s est
    l'unique 6..9 (moyenne 500), qui enjambe le raccord a l'indice 5 seulement si le
    raccord est bien pris en compte. Avec une serie constante, n'importe quelle fenetre
    ferait l'affaire et le test passerait meme si la detection etait cassee.
    """
    # Deux segments recolles apres excision d'une pause : le raccord est a l'indice 5.
    series = [100.0, 100.0, 100.0, 100.0, 400.0, 400.0, 500.0, 500.0, 500.0, 500.0]
    value, spans_pause = best_effort_spans_pause(series, 6, segment_boundaries=[5])
    # Meilleure fenetre de 6 s : indices 4..9 -> (400+400+500*4)/6.
    assert value == pytest.approx((400.0 * 2 + 500.0 * 4) / 6)
    assert spans_pause is True


def test_best_effort_spans_pause_fenetre_collee_au_raccord_sans_le_traverser() -> None:
    """Une fenetre qui s'arrete juste avant le raccord, ou qui demarre dessus, ne l'enjambe pas.

    Cas limite le plus facile a rater : un raccord a l'indice b appartient au segment
    aval ; la fenetre [b, b+d-1] est entierement contigue, la fenetre [b-d, b-1] aussi.
    """
    series = [100.0] * 5 + [500.0] * 5
    value, spans = best_effort_spans_pause(series, 5, segment_boundaries=[5])
    assert value == 500.0  # fenetre 5..9, demarre sur le raccord
    assert spans is False

    value, spans = best_effort_spans_pause(series[::-1], 5, segment_boundaries=[5])
    assert value == 500.0  # fenetre 0..4, s'arrete juste avant le raccord
    assert spans is False


def test_best_effort_spans_pause_duree_hors_de_portee_est_none_et_false() -> None:
    assert best_effort_spans_pause([1.0, 2.0], 5, segment_boundaries=[1]) == (None, False)
    assert best_effort_spans_pause([], 1, segment_boundaries=[]) == (None, False)


def test_best_effort_spans_pause_sans_excision_est_toujours_false() -> None:
    series = [100.0, 400.0, 200.0, 300.0]
    assert best_effort_spans_pause(series, 2, segment_boundaries=[])[1] is False


def test_best_effort_spans_pause_valeur_identique_a_best_effort() -> None:
    """La valeur renvoyee ne doit jamais dependre de la presence de raccords."""
    series = [100.0, 400.0, 200.0, 300.0, 50.0, 600.0, 120.0]
    for d in range(1, len(series) + 1):
        assert best_effort_spans_pause(series, d, segment_boundaries=[3])[0] == best_effort(
            series, d
        )


def test_best_effort_spans_pause_false_quand_la_fenetre_ne_traverse_rien() -> None:
    series = [100.0] * 5 + [500.0] * 5
    value, spans_pause = best_effort_spans_pause(series, 3, segment_boundaries=[5])
    assert value == 500.0
    assert spans_pause is False


def test_power_duration_curve_serie_vide_ne_leve_pas_et_ne_fabrique_rien() -> None:
    curve = power_duration_curve([])
    assert all(value is None for value in curve.values())


def test_power_duration_curve_un_seul_point() -> None:
    curve = power_duration_curve([250.0])
    assert curve == {1: 250.0}


def test_best_effort_accepte_des_valeurs_negatives_sans_les_traiter_comme_absentes() -> None:
    """Serie generique (altitude, temperature...) : -5 est une valeur, pas une absence."""
    assert best_effort([-5.0, -1.0, -3.0], 1) == -1.0
    assert best_effort([-5.0, -5.0], 2) == -5.0


def test_best_effort_toutes_valeurs_nulles_donne_zero_pas_none() -> None:
    """Ici 0 est la reponse juste : la serie existe et vaut 0, ce n'est pas une absence."""
    assert best_effort([0.0] * 10, 5) == 0.0


def test_log_spaced_durations_bornes_invalides_levent_value_error() -> None:
    with pytest.raises(ValueError, match="min_s"):
        log_spaced_durations(0, 100)
    with pytest.raises(ValueError, match="max_s"):
        log_spaced_durations(10, 5)


def test_log_spaced_durations_min_egal_max() -> None:
    assert log_spaced_durations(60, 60) == [60]


def test_log_spaced_durations_couvre_les_durees_usuelles_de_la_spec() -> None:
    """5 s / 60 s / 300 s / 1200 s / 3600 s sont les durees citees au §8.1.

    La grille log ne les contient pas toutes exactement ; on verifie qu'elle en approche
    chacune a moins de 10 %, sinon la courbe affichee serait trompeuse.
    """
    durations = log_spaced_durations(1, 3600)
    for cible in (5, 60, 300, 1200, 3600):
        proche = min(durations, key=lambda d: abs(d - cible))
        assert abs(proche - cible) / cible < 0.10
