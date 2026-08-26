"""Tests for t0.compute.decoupling: cases DECOUP-1 to DECOUP-3.

Spec: 04-load-formulas.md section 7.5.
"""

from __future__ import annotations

import pytest

from t0.compute.decoupling import decoupling_pct, efficiency_factor


def test_decoup1_bounded_values_from_the_spec() -> None:
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(198.0, 148.0)
    assert ef1 == pytest.approx(1.4285714285714286)
    assert ef2 == pytest.approx(1.337837837837838)

    decoupling = decoupling_pct(ef1, ef2)
    assert decoupling == pytest.approx(6.351351351351346)


def test_decoup2_identical_np_and_hr_give_exactly_zero_decoupling() -> None:
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(200.0, 140.0)
    assert decoupling_pct(ef1, ef2) == 0.0


def test_decoup3_missing_hr_gives_none_without_raising() -> None:
    ef1 = efficiency_factor(200.0, 140.0)
    ef2 = efficiency_factor(198.0, None)
    assert ef2 is None
    assert decoupling_pct(ef1, ef2) is None


def test_efficiency_factor_missing_output_is_none() -> None:
    assert efficiency_factor(None, 140.0) is None


def test_efficiency_factor_zero_or_negative_hr_is_none_not_division_by_zero() -> None:
    assert efficiency_factor(200.0, 0.0) is None
    assert efficiency_factor(200.0, -1.0) is None


def test_decoupling_pct_zero_ef1_is_none_not_division_by_zero() -> None:
    assert decoupling_pct(0.0, 1.0) is None
