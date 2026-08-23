"""Tests de garminator.compute.hr_histogram — resolution 1 bpm, pas de pre-binning en zones."""

from __future__ import annotations

from garminator.compute.hr_histogram import hr_histogram


def test_hr_histogram_compte_les_secondes_par_bpm() -> None:
    hr = [140, 140, 141, 141, 141, 150]
    histogram = hr_histogram(hr)
    assert histogram == {140: 2, 141: 3, 150: 1}


def test_hr_histogram_ignore_les_echantillons_absents() -> None:
    """Un capteur debranche (None) ne doit jamais compter comme 0 bpm."""
    hr: list[int | None] = [140, None, None, 141]
    histogram = hr_histogram(hr)
    assert histogram == {140: 1, 141: 1}
    assert 0 not in histogram


def test_hr_histogram_ignore_les_artefacts_hors_plage_plausible() -> None:
    hr = [140, 5, 300, 141]
    histogram = hr_histogram(hr)
    assert histogram == {140: 1, 141: 1}


def test_hr_histogram_bornes_incluses() -> None:
    hr = [20, 260]
    histogram = hr_histogram(hr)
    assert histogram == {20: 1, 260: 1}


def test_hr_histogram_serie_vide_donne_histogramme_vide() -> None:
    assert hr_histogram([]) == {}


def test_hr_histogram_est_falsifiable_pas_une_simple_moyenne() -> None:
    """Deux series de meme longueur, repartition differente -> histogrammes distincts.

    Ce test echouerait si l'implementation retombait par erreur sur un seul agregat
    (moyenne, min/max) au lieu d'un histogramme complet par bpm.
    """
    hr_a = [140] * 10
    hr_b = [130] * 5 + [150] * 5
    assert hr_histogram(hr_a) != hr_histogram(hr_b)
    assert sum(hr_histogram(hr_a).values()) == sum(hr_histogram(hr_b).values()) == 10
