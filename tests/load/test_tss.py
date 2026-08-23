"""Cas numériques auto-portants de 04-formules-charge.md §2-4 (TSS/rTSS/sTSS)."""

from __future__ import annotations

import math

from garminator.load.tss import bike_tss, run_gap_tss, run_power_tss, swim_tss


def _approx(a: float | None, b: float, tol: float = 1e-9) -> bool:
    assert a is not None
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


class TestBikeTss:
    def test_tss_1_if_1(self) -> None:
        result = bike_tss(3600, 250, 250)
        assert _approx(result.intensity_factor, 1.0)
        assert _approx(result.tss, 100.0)

    def test_tss_2_if_88(self) -> None:
        result = bike_tss(3600, 220, 250)
        assert _approx(result.intensity_factor, 0.88)
        assert _approx(result.tss, 77.44)

    def test_tss_3_1h30(self) -> None:
        result = bike_tss(5400, 230, 250)
        assert _approx(result.intensity_factor, 0.92)
        assert _approx(result.tss, 126.96)

    def test_tss_4_ftp_absent_ne_fabrique_rien(self) -> None:
        result = bike_tss(3600, 220, None)
        assert result.intensity_factor is None
        assert result.tss is None

    def test_np_absent_ne_fabrique_rien(self) -> None:
        result = bike_tss(3600, None, 250)
        assert result.intensity_factor is None
        assert result.tss is None

    def test_duree_nulle_ne_fabrique_rien(self) -> None:
        result = bike_tss(0, 220, 250)
        assert result.tss is None


class TestRunPowerTss:
    def test_rtss_p_1(self) -> None:
        result = run_power_tss(2700, 315, 300)
        assert _approx(result.intensity_factor, 1.05)
        assert _approx(result.tss, 82.6875)

    def test_rftp_absent_methode_inactive(self) -> None:
        """rFTP volontairement NULL pour cet athlète (addendum §5) : la méthode
        run_power doit rester inactive, jamais approximée depuis une autre grandeur."""
        result = run_power_tss(2700, 315, None)
        assert result.intensity_factor is None
        assert result.tss is None


class TestRunGapTss:
    def test_rtss_gap_1(self) -> None:
        ftpa_speed = 1000.0 / 240.0
        ngp_speed = 1000.0 / 270.0
        result = run_gap_tss(2700, ngp_speed, ftpa_speed)
        assert _approx(result.intensity_factor, 0.8888888888888888)
        assert _approx(result.tss, 59.25925925925925)


class TestSwimTss:
    def test_stss_1(self) -> None:
        css = 100.0 / 95.0
        result = swim_tss(2100, 2000, css)
        assert _approx(result.intensity_factor, 0.9047619047619048)
        assert _approx(result.tss, 43.203577727387255)

    def test_stss_1_variante_carre_pour_reference(self) -> None:
        css = 100.0 / 95.0
        result = swim_tss(2100, 2000, css, exponent=2)
        assert _approx(result.tss, 47.75132275132275)

    def test_stss_2_axiome_1h_au_seuil(self) -> None:
        css = 1.0
        result = swim_tss(3600, 3600, css)
        assert _approx(result.intensity_factor, 1.0)
        assert _approx(result.tss, 100.0)

    def test_swim_time_doit_etre_le_temps_actif(self) -> None:
        """swim_time_s=None (pas de temps de nage actif connu) ne doit jamais retomber
        silencieusement sur une autre durée : sTSS reste None."""
        result = swim_tss(None, 2000, 1.0)
        assert result.tss is None

    def test_css_absent_ne_fabrique_rien(self) -> None:
        result = swim_tss(2100, 2000, None)
        assert result.tss is None


class TestDegenerateThresholds:
    """04 §2.3 : un seuil nul, négatif ou une durée négative ne doivent jamais produire
    un nombre. La division par zéro et les IF négatifs sont les deux façons dont un
    seuil dégénéré contamine silencieusement toute l'histoire."""

    def test_ftp_nul_ne_fabrique_rien(self) -> None:
        result = bike_tss(3600, 250, 0.0)
        assert result.tss is None
        assert result.intensity_factor is None

    def test_ftp_negatif_ne_fabrique_rien(self) -> None:
        result = bike_tss(3600, 250, -250.0)
        assert result.tss is None
        assert result.intensity_factor is None

    def test_rftp_nul_ne_fabrique_rien(self) -> None:
        assert run_power_tss(3600, 250, 0.0).tss is None

    def test_vitesse_seuil_nulle_ne_fabrique_rien(self) -> None:
        assert run_gap_tss(3600, 3.5, 0.0).tss is None

    def test_css_nul_ne_fabrique_rien(self) -> None:
        assert swim_tss(1800, 1800.0, 0.0).tss is None

    def test_css_negatif_ne_fabrique_rien(self) -> None:
        assert swim_tss(1800, 1800.0, -1.0).tss is None

    def test_puissance_negative_ne_fabrique_rien(self) -> None:
        """Un NP négatif est physiquement impossible : le rejeter plutôt que de
        produire un IF négatif dont le carré redeviendrait positif et plausible."""
        assert bike_tss(3600, -230.0, 250.0).tss is None

    def test_duree_negative_ne_fabrique_rien(self) -> None:
        assert bike_tss(-3600, 250.0, 250.0).tss is None

    def test_distance_nulle_ne_fabrique_rien(self) -> None:
        assert swim_tss(1800, 0.0, 1.0).tss is None

    def test_swim_time_nul_ne_fabrique_rien(self) -> None:
        assert swim_tss(0, 1800.0, 1.0).tss is None
