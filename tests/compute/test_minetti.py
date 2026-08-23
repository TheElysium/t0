"""Tests de garminator.compute.minetti — table Minetti et cas GAP-1/GAP-2.

Les valeurs numeriques de la table ci-dessous sont calculees directement depuis le
polynome de la specification (04-formules-charge.md §3.3) avec pytest.approx pour
tolerer les arrondis flottants ; les cas GAP-1/GAP-2 recopient les valeurs bornees
donnees en toutes lettres par la specification.
"""

from __future__ import annotations

import pytest

from garminator.compute.minetti import (
    grade_adjusted_pace_s_per_km,
    grade_adjusted_speed_mps,
    grade_adjustment_factor,
    normalized_graded_pace_s_per_km,
    normalized_graded_pace_speed_mps,
    running_energy_cost,
    smooth_grade_series,
)

# (pente i, EC(i) attendu J/kg/m, f(i) attendu) -- recalcule depuis le polynome de la spec.
_MINETTI_TABLE: list[tuple[float, float, float]] = [
    (-0.30, 2.4622379999999997, 0.6839549999999999),
    (-0.25, 2.0248046875, 0.5624457465277778),
    (-0.20, 1.8000320000000003, 0.500008888888889),
    (-0.15, 1.8356968125000002, 0.50991578125),
    (-0.10, 2.151706, 0.5976961111111111),
    (-0.05, 2.7459239374999997, 0.7627566493055554),
    (0.00, 3.6, 1.0),
    (0.05, 4.6851960625, 1.3014433506944445),
    (0.10, 5.968214, 1.6578372222222222),
    (0.15, 7.4170231875, 2.06028421875),
    (0.20, 9.006688, 2.501857777777778),
    (0.25, 10.7251953125, 2.979220920138889),
    (0.30, 12.579282, 3.494245),
]


@pytest.mark.parametrize(("grade", "expected_ec", "expected_f"), _MINETTI_TABLE)
def test_running_energy_cost_et_grade_adjustment_factor(
    grade: float, expected_ec: float, expected_f: float
) -> None:
    assert running_energy_cost(grade) == pytest.approx(expected_ec)
    assert grade_adjustment_factor(grade) == pytest.approx(expected_f)


def test_ec_zero_est_le_cout_de_reference() -> None:
    assert running_energy_cost(0.0) == pytest.approx(3.6)
    assert grade_adjustment_factor(0.0) == pytest.approx(1.0)


def test_ec_non_monotone_en_descente_minimum_vers_moins_0_18() -> None:
    """Propriete de non-regression clef : la descente n'est pas toujours moins couteuse.

    Une implementation naive « plus ca descend moins ca coute » echouerait ici : le
    cout remonte au-dela d'environ -18% de pente (freinage excentrique, Minetti 2002).
    """
    assert running_energy_cost(-0.30) > running_energy_cost(-0.20)
    assert running_energy_cost(-0.30) > running_energy_cost(-0.15)


def test_clamp_pratique_a_0_30_au_dela_du_domaine() -> None:
    """Une pente au-dela du clamp pratique est ramenee a ±0.30 avant evaluation (§3.3.1)."""
    assert running_energy_cost(0.50) == pytest.approx(running_energy_cost(0.30))
    assert running_energy_cost(-0.50) == pytest.approx(running_energy_cost(-0.30))


def test_gap1_pente_positive_5_pourcent() -> None:
    """GAP-1 : v=3.7037037... m/s (allure 4:30/km), pente +5% -> valeurs bornees de la spec."""
    v = 1000.0 / 270.0  # 4:30 min/km = 270 s/km
    f = grade_adjustment_factor(0.05)
    assert f == pytest.approx(1.3014433506944445)

    v_gap = grade_adjusted_speed_mps(v, 0.05)
    assert v_gap == pytest.approx(4.820160558127572)

    pace = grade_adjusted_pace_s_per_km(v, 0.05)
    assert pace == pytest.approx(207.4619689408142)


def test_gap2_pente_negative_confortable_diminue_l_allure_gap() -> None:
    """GAP-2 : une pente negative moderee (dans la zone benefique) donne v_gap < v."""
    v = 3.0
    v_gap = grade_adjusted_speed_mps(v, -0.05)
    assert v_gap < v


def test_grade_adjusted_pace_none_si_vitesse_ajustee_non_positive() -> None:
    assert grade_adjusted_pace_s_per_km(0.0, 0.0) is None


def test_smooth_grade_series_plat_donne_pente_nulle() -> None:
    distance = [float(i) for i in range(60)]
    altitude = [100.0] * 60
    grades = smooth_grade_series(distance, altitude)
    non_none = [g for g in grades if g is not None]
    assert non_none
    assert all(abs(g) < 1e-9 for g in non_none)


def test_smooth_grade_series_montee_lineaire_donne_pente_positive_constante() -> None:
    """1 m de denivele par metre parcouru sur une longue montee -> pente clampee au centre."""
    distance = [float(i) for i in range(100)]
    altitude = [i * 0.5 for i in range(100)]  # 0.5 m de D+ par metre -> pente 50%
    grades = smooth_grade_series(distance, altitude)
    center = grades[50]
    assert center is not None
    assert center == pytest.approx(0.30)  # clampe a 0.30, pente reelle = 0.5


def test_smooth_grade_series_arret_donne_none() -> None:
    """A l'arret (distance constante), la pente est indefinie -> None, jamais devinee."""
    distance = [0.0] * 30
    altitude = [100.0] * 30
    grades = smooth_grade_series(distance, altitude)
    assert all(g is None for g in grades)


def test_smooth_grade_series_altitude_absente_donne_none() -> None:
    distance = [float(i) for i in range(20)]
    altitude: list[float | None] = [None] * 20
    grades = smooth_grade_series(distance, altitude)
    assert all(g is None for g in grades)


def test_normalized_graded_pace_moins_de_30_echantillons_est_none() -> None:
    speed = [3.0] * 29
    grade = [0.0] * 29
    assert normalized_graded_pace_speed_mps(speed, grade) is None
    assert normalized_graded_pace_s_per_km(speed, grade) is None


def test_normalized_graded_pace_plat_egale_np_vitesse_plate() -> None:
    """Sur du plat pur (pente nulle partout), la NGP en vitesse degenere en NP-vitesse classique."""
    speed = [3.0] * 60
    grade = [0.0] * 60
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    assert ngp == pytest.approx(3.0)


def test_normalized_graded_pace_grade_none_traite_comme_plat() -> None:
    speed = [3.0] * 60
    grade: list[float | None] = [None] * 60
    ngp = normalized_graded_pace_speed_mps(speed, grade)
    assert ngp == pytest.approx(3.0)


def test_minimum_du_modele_est_bien_localise_vers_moins_0_1814() -> None:
    """Localisation numerique du minimum : i ~ -0.1814, EC ~ 1.78127 (§3.3.1).

    Recalcule ici par balayage fin, sans faire confiance a la table transcrite : c'est
    la propriete non triviale du modele (freinage excentrique en forte descente).
    """
    grid = [-0.30 + k * 0.0001 for k in range(2501)]  # -0.30 .. -0.05
    i_min = min(grid, key=running_energy_cost)
    assert i_min == pytest.approx(-0.1814, abs=5e-4)
    assert running_energy_cost(i_min) == pytest.approx(1.78127, abs=1e-4)
    # f(-0.20) = 0.5000 est le point de controle commode de la spec.
    assert grade_adjustment_factor(-0.20) == pytest.approx(0.5, abs=1e-4)


def test_clamp_est_un_plateau_pas_une_extrapolation() -> None:
    """Au-dela de +-0.30 le polynome n'est plus evalue : le facteur devient constant.

    Comportement documente (docstring du module + de running_energy_cost). Le polynome
    non clampe donnerait EC(0.45) = 19.43 contre EC(0.30) = 12.58, soit +54 % : si un
    jour le clamp saute, ce test le signale.
    """
    assert running_energy_cost(0.45) == running_energy_cost(0.30)
    assert running_energy_cost(10.0) == running_energy_cost(0.30)
    assert running_energy_cost(-10.0) == running_energy_cost(-0.30)
    polynome_non_clampe = (
        155.4 * 0.45**5 - 30.4 * 0.45**4 - 43.3 * 0.45**3 + 46.3 * 0.45**2 + 19.5 * 0.45 + 3.6
    )
    assert polynome_non_clampe == pytest.approx(19.4260145625)
    assert running_energy_cost(0.45) < polynome_non_clampe


def test_normalized_graded_pace_exclut_les_echantillons_a_vitesse_nulle() -> None:
    """§3.3.4 : v = 0 (feu rouge) n'est pas l'equivalent de p = 0 (roue libre).

    Les conserver tirerait la NGP vers le bas sans contenu physiologique. Asymetrie
    delibaree avec §1.4, testee ici : inserer des arrets ne doit pas changer la NGP.
    """
    speed = [3.0] * 60
    grade: list[float | None] = [0.0] * 60
    reference = normalized_graded_pace_speed_mps(speed, grade)
    assert reference == pytest.approx(3.0)

    avec_arrets = [3.0] * 30 + [0.0] * 20 + [3.0] * 30
    grade_arrets: list[float | None] = [0.0] * 80
    assert normalized_graded_pace_speed_mps(avec_arrets, grade_arrets) == pytest.approx(3.0)


def test_normalized_graded_pace_moins_de_30_echantillons_exploitables_est_none() -> None:
    """40 echantillons dont 20 a l'arret -> 20 exploitables -> None, pas une NGP fabriquee."""
    speed = [3.0] * 20 + [0.0] * 20
    grade: list[float | None] = [0.0] * 40
    assert normalized_graded_pace_speed_mps(speed, grade) is None
    assert normalized_graded_pace_s_per_km(speed, grade) is None


def test_normalized_graded_pace_series_vides_et_incoherentes() -> None:
    assert normalized_graded_pace_speed_mps([], []) is None
    with pytest.raises(ValueError, match="longueur"):
        normalized_graded_pace_speed_mps([3.0], [])


def test_normalized_graded_pace_montee_augmente_la_ngp() -> None:
    """Falsifiabilite : a vitesse egale, une montee doit relever la NGP (f > 1)."""
    speed = [3.0] * 60
    plat: list[float | None] = [0.0] * 60
    montee: list[float | None] = [0.10] * 60
    ngp_plat = normalized_graded_pace_speed_mps(speed, plat)
    ngp_montee = normalized_graded_pace_speed_mps(speed, montee)
    assert ngp_plat is not None
    assert ngp_montee is not None
    assert ngp_montee == pytest.approx(3.0 * 1.6578372222222222)
    assert ngp_montee > ngp_plat
    # ... et l'allure correspondante doit etre plus RAPIDE (moins de s/km).
    pace_plat = normalized_graded_pace_s_per_km(speed, plat)
    pace_montee = normalized_graded_pace_s_per_km(speed, montee)
    assert pace_plat is not None
    assert pace_montee is not None
    assert pace_montee < pace_plat


def test_smooth_grade_series_lisse_le_bruit_altimetrique() -> None:
    """§3.3.2 etape 1 : l'altitude doit etre moyennee avant la difference centree.

    Terrain plat + bruit barometrique en dents de scie de +-0.5 m. Sans lissage, la
    difference brute alt[hi]-alt[lo] vaut +-1 m sur ~20 m, soit +-5 % de pente. Avec
    lissage, le bruit s'annule presque.
    """
    distance = [float(i) for i in range(80)]
    altitude: list[float | None] = [100.0 + (0.5 if i % 2 == 0 else -0.5) for i in range(80)]
    grades = smooth_grade_series(distance, altitude)
    centre = [g for g in grades[20:60] if g is not None]
    assert len(centre) == 40
    assert max(abs(g) for g in centre) < 0.01

    # Preuve que le bruit est bien present et que le test n'est pas vide de sens :
    # la difference BRUTE sur la meme fenetre vaut 1 m pour 20 m, soit 5 %.
    alt_hi, alt_lo = altitude[30], altitude[9]
    assert alt_hi is not None
    assert alt_lo is not None
    brut = (alt_hi - alt_lo) / (distance[30] - distance[9])
    assert abs(brut) == pytest.approx(1.0 / 21.0, abs=1e-9)


def test_smooth_grade_series_longueurs_incoherentes_leve_value_error() -> None:
    with pytest.raises(ValueError, match="longueur"):
        smooth_grade_series([0.0, 1.0], [100.0])


def test_smooth_grade_series_serie_vide() -> None:
    assert smooth_grade_series([], []) == []


def test_smooth_grade_series_altitude_partiellement_absente() -> None:
    """Un trou d'altitude ne doit pas etre compte comme 0 m dans la moyenne glissante."""
    distance = [float(i) for i in range(60)]
    altitude: list[float | None] = [float(i) * 0.05 for i in range(60)]
    altitude[30] = None
    grades = smooth_grade_series(distance, altitude)
    assert grades[30] is None  # altitude absente a cet echantillon -> pente inconnue
    voisin = grades[20]
    assert voisin is not None
    assert voisin == pytest.approx(0.05, abs=5e-3)


def test_smooth_grade_series_altitude_isolee_reste_exploitable() -> None:
    """Une unique altitude connue suffit : lo et hi sont a <= half_window_m de k, donc
    leurs propres fenetres contiennent k et la moyenne lissee y est definie.

    Consequence : la pente vaut 0 (memes altitudes lissees aux deux bornes), ce qui est
    l'information juste — il n'y a aucun denivele mesure entre les bornes.
    """
    distance = [float(i) * 5.0 for i in range(20)]
    altitude: list[float | None] = [None] * 20
    altitude[10] = 100.0
    grades = smooth_grade_series(distance, altitude, half_window_m=10.0)
    assert grades[10] == pytest.approx(0.0)
    assert grades[0] is None  # altitude absente a cet echantillon
