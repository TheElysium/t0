"""Tests de garminator.compute.normalized_power — cas NP-0 à NP-4 de 04-formules-charge.md.

Toutes les valeurs numeriques attendues sont recopiees depuis la specification.
"""

from __future__ import annotations

from garminator.compute.normalized_power import (
    RELIABLE_THRESHOLD_S,
    average,
    average_nonzero,
    normalized_power,
    np_reliable,
)


def test_np0_moins_de_30_echantillons_est_none() -> None:
    """NP-0 : une serie de moins de 30 s ne definit pas de fenetre pleine -> None, jamais 0."""
    p = [200.0] * 29
    assert normalized_power(p) is None


def test_np0_serie_constante_degeneree() -> None:
    """NP-0 : serie constante de 3600 s -> NP = avg = 250.0 exactement (§1.6)."""
    p = [250.0] * 3600
    assert normalized_power(p) == 250.0
    assert average(p) == 250.0


def test_np0_exactement_30_echantillons_est_definie() -> None:
    p = [200.0] * 30
    assert normalized_power(p) == 200.0


def test_np1_mecanique_de_l_algorithme_hand_traced() -> None:
    """NP-1 : W=2 (fenetre reduite pour la tracabilite manuelle), verifiable a la main (§1.6)."""
    p = [100.0, 200.0, 300.0, 400.0]
    result = normalized_power(p, window_s=2)
    assert result is not None
    assert abs(result - 283.6449161403191) < 1e-6


def test_np2_deux_plateaux_120s() -> None:
    """NP-2 : 60 s a 200 W puis 60 s a 300 W, fenetre standard 30 s."""
    p = [200.0] * 60 + [300.0] * 60
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 261.029088063548) < 1e-6
    assert average(p) == 250.0


def test_np3_puissance_puis_arret_zeros_conserves() -> None:
    """NP-3 : 30 s a 300 W puis 30 s a 0 W — les zeros de roue libre sont conserves, pas exclus."""
    p = [300.0] * 30 + [0.0] * 30
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 203.09282812902046) < 1e-6
    assert average(p) == 150.0


def test_np4_np_reliable_sous_le_seuil() -> None:
    """NP-4 : entre 30 et 300 s exploitables, NP est definie mais peu fiable."""
    assert np_reliable(30) is False
    assert np_reliable(RELIABLE_THRESHOLD_S - 1) is False
    assert np_reliable(RELIABLE_THRESHOLD_S) is True
    assert np_reliable(29) is None


def test_average_nonzero_exclut_les_zeros() -> None:
    p = [0.0, 0.0, 100.0, 200.0]
    assert average_nonzero(p) == 150.0
    assert average(p) == 75.0


def test_average_serie_vide_est_none_jamais_zero() -> None:
    assert average([]) is None
    assert average_nonzero([]) is None


def test_normalized_power_est_falsifiable_ordre_des_puissances_compte() -> None:
    """Deux series de meme moyenne mais de variance differente doivent donner des NP differentes.

    Ce test echouerait si l'implementation retombait par erreur sur une simple moyenne
    (bug frequent : oublier la puissance 4 / racine 4e).
    """
    flat = [250.0] * 60
    variable = [100.0] * 30 + [400.0] * 30
    assert average(flat) == average(variable) == 250.0
    np_flat = normalized_power(flat)
    np_variable = normalized_power(variable)
    assert np_flat is not None
    assert np_variable is not None
    assert np_flat == 250.0
    assert np_variable > np_flat


def test_normalized_power_serie_vide_est_none() -> None:
    assert normalized_power([]) is None


def test_normalized_power_serie_integralement_nulle_est_none_jamais_zero() -> None:
    """§1.5 : « toutes valeurs nulles » -> NULL.

    Une serie a 0 W du debut a la fin n'est pas un effort a 0 W, c'est un capteur
    absent. Renvoyer 0.0 ferait entrer IF = 0 puis TSS = 0 dans L5 sans qu'aucun
    consommateur en aval puisse distinguer ce 0 d'une valeur mesuree.
    """
    assert normalized_power([0.0] * 3600) is None
    assert average_nonzero([0.0] * 3600) is None
    # Un seul watt non nul suffit a rendre la serie exploitable.
    serie = [0.0] * 3599 + [1.0]
    resultat = normalized_power(serie)
    assert resultat is not None
    assert resultat > 0.0


def test_normalized_power_un_seul_echantillon_est_none() -> None:
    assert normalized_power([250.0]) is None
    assert normalized_power([250.0], window_s=1) == 250.0


def test_np_reliable_ne_masque_pas_une_serie_trop_courte() -> None:
    """np_reliable doit dire None (et non False) quand NP elle-meme est None.

    False signifierait « NP existe mais est fragile » ; None signifie « il n'y a pas
    de NP du tout ». Les deux ne se traitent pas pareil en aval.
    """
    for count in (0, 1, 29):
        assert np_reliable(count) is None
        assert normalized_power([200.0] * count) is None


def test_np2_convention_de_bord_a_est_bien_celle_implementee() -> None:
    """La convention (B) « fenetre extensible » donnerait 250.016, pas 261.029 (§1.6).

    Ecart de 4,4 % : ce test echouerait si quelqu'un « corrigeait » le traitement des
    bords en produisant aussi les fenetres incompletes du debut de serie.
    """
    p = [200.0] * 60 + [300.0] * 60
    result = normalized_power(p)
    assert result is not None
    assert abs(result - 261.029088063548) < 1e-9
    assert abs(result - 250.01614656735006) > 10.0
