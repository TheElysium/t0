"""Cas numériques auto-portants de 04-formules-charge.md §5.1 (TRIMP de Banister)."""

from __future__ import annotations

import math

from garminator.load.trimp import trimp_from_histogram


def _histogram_constant(hr_bpm: int, seconds: int) -> dict[int, int]:
    return {hr_bpm: seconds}


class TestTrimp:
    def test_trimp_1_homme(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is not None
        assert math.isclose(value, 95.88495346861073, rel_tol=1e-9)

    def test_trimp_2_femme_coefficient_different(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="female")
        assert value is not None
        assert math.isclose(value, 108.74116520702313, rel_tol=1e-9)
        # Falsifiabilité : la variante femme doit différer de la variante homme, sinon
        # le paramètre sex ne serait pas réellement pris en compte par le calcul.
        male_value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value != male_value

    def test_trimp_3_au_seuil_lthr(self) -> None:
        """1h au LTHR (170 bpm) donne ~170 TRIMP, PAS 100 : le point clé de la spec est
        que TRIMP vit sur une échelle différente de TSS et ne doit jamais être sommé
        avec lui dans la même colonne de charge."""
        histogram = _histogram_constant(170, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is not None
        assert math.isclose(value, 170.651090478826, rel_tol=1e-9)

    def test_fc_absente_donne_none_jamais_zero(self) -> None:
        """Règle métier non négociable : sans FC exploitable, TRIMP doit être None,
        jamais 0.0 -- un zéro serait lu comme « séance sans effort »."""
        value = trimp_from_histogram({}, hr_rest_bpm=50, hr_max_bpm=190, sex="male")
        assert value is None

    def test_seuils_absents_donne_none_jamais_zero(self) -> None:
        histogram = _histogram_constant(145, 3600)
        assert trimp_from_histogram(histogram, hr_rest_bpm=None, hr_max_bpm=190) is None
        assert trimp_from_histogram(histogram, hr_rest_bpm=50, hr_max_bpm=None) is None

    def test_hr_max_incoherent_donne_none(self) -> None:
        histogram = _histogram_constant(145, 3600)
        value = trimp_from_histogram(histogram, hr_rest_bpm=190, hr_max_bpm=50)
        assert value is None

    def test_croissant_avec_intensite(self) -> None:
        """Falsifiabilité : un histogramme à intensité plus élevée doit produire un
        TRIMP strictement supérieur (fonction non triviale, pas une constante)."""
        low = trimp_from_histogram({120: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        high = trimp_from_histogram({170: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        assert low is not None
        assert high is not None
        assert high > low

    def test_hrr_borne_a_1_au_dela_de_hr_max(self) -> None:
        at_max = trimp_from_histogram({190: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        above_max = trimp_from_histogram({210: 3600}, hr_rest_bpm=50, hr_max_bpm=190)
        assert at_max is not None
        assert above_max is not None
        assert math.isclose(at_max, above_max, rel_tol=1e-9)
