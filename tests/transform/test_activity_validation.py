"""Garde-fous de ``build_core_rows`` : deux incohérences d'appel qui doivent être
détectées plutôt que silencieusement ignorées (cf. docstring de la fonction)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from garminator.transform.activity import build_core_rows

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    bike_context,
    make_sample,
    parsed_source,
)


class TestGardeFousBuildCoreRows:
    def test_duree_nulle_ou_negative_leve_value_error(self) -> None:
        decoded = parsed_source([])
        with pytest.raises(ValueError, match="duration_s"):
            build_core_rows(
                decoded,
                bike_context(duration_s=0),
                athlete_id=ATHLETE_ID,
                fit_file_id=None,
                threshold=BIKE_THRESHOLD,
                all_threshold=ALL_THRESHOLD,
                sex="male",
            )

    def test_incoherence_activity_id_entre_contexte_et_echantillons_leve_value_error(self) -> None:
        # Un échantillon qui porte l'activity_id d'une AUTRE activité : le genre de bug
        # qu'on veut détecter, pas ignorer (cf. docstring de build_core_rows).
        sample = make_sample(0, power=200)
        wrong_activity_sample = replace(sample, activity_id=999)
        decoded = parsed_source([wrong_activity_sample])
        with pytest.raises(ValueError, match="activity_id"):
            build_core_rows(
                decoded,
                bike_context(activity_id=1, duration_s=600),
                athlete_id=ATHLETE_ID,
                fit_file_id=None,
                threshold=BIKE_THRESHOLD,
                all_threshold=ALL_THRESHOLD,
                sex="male",
            )
