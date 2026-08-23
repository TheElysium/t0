"""Tests de garminator.compute.decoupling — cas DECOUP-1 a DECOUP-3 (04-formules-charge.md §7.5)."""

from __future__ import annotations

import pytest

from garminator.compute.decoupling import decoupling_pct, efficiency_factor


def test_decoup1_valeurs_bornees_de_la_spec() -> None:
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(198.0, 148.0)
    assert ef1 == pytest.approx(1.4285714285714286)
    assert ef2 == pytest.approx(1.337837837837838)

    decoupling = decoupling_pct(ef1, ef2)
    assert decoupling == pytest.approx(6.351351351351346)


def test_decoup2_invariant_np_et_hr_identiques() -> None:
    """Meme NP et meme FC sur les deux moities -> decouplage exactement nul."""
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(200.0, 140.0)
    assert decoupling_pct(ef1, ef2) == 0.0


def test_decoup3_fc_manquante_donne_none_sans_exception() -> None:
    """FC absente en seconde moitie -> EF2 est None -> decoupling est None, pas d'exception."""
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(198.0, None)
    assert ef2 is None
    assert decoupling_pct(ef1, ef2) is None


def test_efficiency_factor_output_manquant_est_none() -> None:
    assert efficiency_factor(None, 140.0) is None


def test_efficiency_factor_fc_nulle_ou_negative_est_none_pas_division_par_zero() -> None:
    assert efficiency_factor(200.0, 0.0) is None
    assert efficiency_factor(200.0, -1.0) is None


def test_decoupling_pct_ef1_nul_est_none_pas_division_par_zero() -> None:
    assert decoupling_pct(0.0, 1.0) is None
