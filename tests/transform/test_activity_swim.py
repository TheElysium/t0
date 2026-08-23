"""Natation : ``core.sample`` est vide par construction (cf. docstring de
``core.swim_length`` dans migrations/004 -- toute la donnée seconde par seconde est
dans les messages ``length``, jamais dans des échantillons GPS/capteur comme les
autres disciplines). Vérifie que zéro échantillon n'est jamais traité comme une
erreur, et que la reconstruction du temps de nage bascule correctement entre les deux
sources possibles (longueurs FIT vs. chrono total)."""

from __future__ import annotations

from datetime import timedelta

from garminator.fit.model import SwimLengthRow
from garminator.transform.activity import build_core_rows

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    START,
    SWIM_THRESHOLD,
    make_sample,
    parsed_source,
    swim_context,
)


def _swim_lengths(count: int, duration_s: float) -> list[SwimLengthRow]:
    return [
        SwimLengthRow(
            activity_id=1,
            length_index=i,
            start_time=START + timedelta(seconds=i * duration_s),
            duration_s=duration_s,
            length_type="active",
        )
        for i in range(count)
    ]


class TestNatationSansEchantillonsAvecLongueurs:
    """Piscine : messages ``length`` disponibles -- source de temps la plus fiable."""

    def test_temps_de_nage_reconstruit_depuis_les_longueurs_actives(self) -> None:
        lengths = _swim_lengths(40, 25.0)  # 40 x 25 m, 25 s/longueur -> 1000 s.
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_s == 1000.0
        assert bundle.activity.swim_time_source == "fit_lengths"
        assert bundle.activity.total_samples == 0
        assert bundle.samples == ()

    def test_data_quality_non_plafonnee_a_fair_quand_les_longueurs_existent(self) -> None:
        lengths = _swim_lengths(4, 25.0)
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=200),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        # Aucun échantillon de puissance -> canal régularisé vide -> qualité "unknown",
        # jamais forcée à "fair" (ce plafond ne vise QUE le repli sur le temps chrono).
        assert bundle.activity.data_quality == "unknown"


class TestNatationEauLibreSansLongueurs:
    """Eau libre : aucun message ``length`` -- repli sur le temps chrono, moins fiable."""

    def test_temps_de_nage_replie_sur_le_chrono_total(self) -> None:
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_s == 1800.0
        assert bundle.activity.swim_time_source == "elapsed"

    def test_data_quality_plafonnee_a_fair_quand_le_canal_puissance_est_bon(self) -> None:
        # Cas rare mais possible (montre connectée avec estimateur de puissance en
        # nage) : le canal puissance seul serait "good", mais le repli chrono sur le
        # temps de nage reste une approximation -- le plafond §4.3 s'applique quand
        # même, canal puissance ou pas.
        samples = [make_sample(t, power=100) for t in range(60)]
        decoded = parsed_source(samples, swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800, activity_id=1),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.activity.swim_time_source == "elapsed"
        assert bundle.activity.data_quality == "fair"

    def test_longueurs_idle_ignorees_dans_la_reconstruction(self) -> None:
        lengths = [
            SwimLengthRow(
                activity_id=1,
                length_index=0,
                start_time=START,
                duration_s=10.0,
                length_type="idle",  # virage/pause : jamais compté comme nage active.
            )
        ]
        decoded = parsed_source([], swim_lengths=lengths)
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        # Aucune longueur "active" -> repli chrono, pas une longueur idle de 10 s.
        assert bundle.activity.swim_time_source == "elapsed"
        assert bundle.activity.swim_time_s == 1800.0

    def test_zero_echantillon_n_est_pas_une_erreur_histogramme_et_courbe_vides(self) -> None:
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert bundle.hr_histogram == ()
        assert bundle.power_curve == ()
        assert bundle.laps == ()

    def test_decouplage_hors_perimetre_en_natation(self) -> None:
        # Descope explicite (cf. docstring de _load_extra_metric_rows) : aucune
        # formule de découplage n'est spécifiée pour la natation dans ce lot.
        decoded = parsed_source([], swim_lengths=[])
        bundle = build_core_rows(
            decoded,
            swim_context(duration_s=1800),
            athlete_id=ATHLETE_ID,
            fit_file_id=None,
            threshold=SWIM_THRESHOLD,
            all_threshold=ALL_THRESHOLD,
            sex="male",
        )
        assert [row for row in bundle.metrics if row.metric == "decoupling_pct"] == []
        assert [row for row in bundle.metrics if row.metric == "efficiency_factor"] == []
