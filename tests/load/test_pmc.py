"""Self-contained numeric cases from 04-formules-charge.md §6 (CTL/ATL/TSB)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from garminator.load.pmc import STATIONARY_SEED_DAYS, dense_daily_tss, run_pmc, stationary_seed

_DAYS_PER_YEAR = 365
_FIVE_YEARS = 5 * _DAYS_PER_YEAR


class TestCtlAtl:
    def test_ctl_1_42_jours_a_100(self) -> None:
        days = run_pmc([100.0] * 42, seed_ctl=0.0, seed_atl=0.0)
        assert math.isclose(days[-1].ctl, 63.65440478283971, rel_tol=1e-9)

    def test_atl_1_7_jours_a_100(self) -> None:
        days = run_pmc([100.0] * 7, seed_ctl=0.0, seed_atl=0.0)
        assert math.isclose(days[-1].atl, 66.00833229108862, rel_tol=1e-9)

    def test_ctl_2_semaine_realiste_table_complete(self) -> None:
        """Reproduces line-by-line CTL-2 example table (04 §6.3)."""
        daily_tss = [0.0, 60.0, 0.0, 90.0, 40.0, 0.0, 120.0]
        expected = [
            (0.000, 0.000),
            (1.429, 8.571),
            (1.395, 7.347),
            (3.504, 19.155),
            (4.373, 22.132),
            (4.269, 18.971),
            (7.025, 33.403),
        ]
        days = run_pmc(daily_tss, seed_ctl=0.0, seed_atl=0.0)
        for day, (expected_ctl, expected_atl) in zip(days, expected, strict=True):
            assert math.isclose(day.ctl, expected_ctl, abs_tol=5e-4)
            assert math.isclose(day.atl, expected_atl, abs_tol=5e-4)

    def test_tsb_1_convention_veille(self) -> None:
        """TSB at day 7 = CTL_previous - ATL_previous (TrainingPeaks convention),
        approx -14.702, NOT CTL_day7 - ATL_day7."""
        daily_tss = [0.0, 60.0, 0.0, 90.0, 40.0, 0.0, 120.0]
        days = run_pmc(daily_tss, seed_ctl=0.0, seed_atl=0.0)
        assert math.isclose(days[6].tsb, -14.701631246386905, rel_tol=1e-6)

    def test_ctl_3_biais_initialisation(self) -> None:
        days = run_pmc([0.0] * 30, seed_ctl=60.0, seed_atl=60.0)
        assert math.isclose(days[-1].ctl, 29.119790089871387, rel_tol=1e-9)

    def test_seed_negatif_refuse(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            run_pmc([0.0], seed_ctl=-1.0, seed_atl=0.0)


class TestReliabilityMasking:
    def test_masque_les_180_premiers_jours(self) -> None:
        days = run_pmc([50.0] * 200, seed_ctl=0.0, seed_atl=0.0)
        assert days[0].ctl_reliable is False
        assert days[179].ctl_reliable is False
        assert days[180].ctl_reliable is True

    def test_pas_de_masquage_avant_le_seed(self) -> None:
        """Falsifiability: first day (seed day inclusive, day_index=1 at local_date=seed_date)
        must never be marked reliable."""
        days = run_pmc([50.0] * 10, seed_ctl=0.0, seed_atl=0.0)
        assert all(not day.ctl_reliable for day in days)


class TestNoDivergenceOverFiveYears:
    def test_la_recurrence_reste_bornee_sur_5_ans(self) -> None:
        """Closed form diverges to 1.26e19 over 5 years (04 §6.7)—forbidden here.
        Recurrence must stay bounded regardless of history: with daily TSS alternating 0/150,
        CTL/ATL must stay physically plausible (no exponential drift)."""
        daily_tss = [150.0 if i % 2 == 0 else 0.0 for i in range(_FIVE_YEARS)]
        days = run_pmc(daily_tss, seed_ctl=0.0, seed_atl=0.0)
        for day in days:
            assert math.isfinite(day.ctl)
            assert math.isfinite(day.atl)
            assert 0.0 <= day.ctl <= 150.0
            assert 0.0 <= day.atl <= 150.0
        # Series converges to steady state (asymptote around daily average 75),
        # proof recurrence stays bounded.
        last_ctl_values = [day.ctl for day in days[-60:]]
        assert max(last_ctl_values) - min(last_ctl_values) < 5.0

    def test_constant_tss_converge_vers_lui_meme(self) -> None:
        """Falsifiability: constant daily TSS over long period must converge CTL/ATL
        to same value (structural property of damped exponential recurrence, not coincidence)."""
        days = run_pmc([80.0] * _FIVE_YEARS, seed_ctl=0.0, seed_atl=0.0)
        assert math.isclose(days[-1].ctl, 80.0, abs_tol=1e-6)
        assert math.isclose(days[-1].atl, 80.0, abs_tol=1e-6)


class TestStationarySeed:
    def test_moyenne_simple(self) -> None:
        ctl, atl = stationary_seed([10.0, 20.0, 30.0])
        assert ctl == 20.0
        assert atl == 20.0

    def test_serie_vide_refusee(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            stationary_seed([])

    def test_fenetre_tronquee_a_42_jours(self) -> None:
        """Strategy named 'stationary_42d': passing entire history must NOT yield annual
        average. Series constructed so mean of first 42 days (100) differs clearly from
        global mean."""
        history = [100.0] * STATIONARY_SEED_DAYS + [0.0] * 323
        ctl, atl = stationary_seed(history)
        assert math.isclose(ctl, 100.0, rel_tol=1e-12)
        assert math.isclose(atl, 100.0, rel_tol=1e-12)
        # Falsifiability: without window would get annual average.
        assert not math.isclose(ctl, sum(history) / len(history), rel_tol=1e-3)

    def test_serie_plus_courte_que_la_fenetre_acceptee(self) -> None:
        ctl, _ = stationary_seed([30.0, 50.0])
        assert math.isclose(ctl, 40.0, rel_tol=1e-12)

    def test_fenetre_invalide_refusee(self) -> None:
        with pytest.raises(ValueError, match="window"):
            stationary_seed([10.0], window_days=0)


class TestDenseCalendar:
    """Le calendrier dense est le point de défaillance nommé en 04 §6.4 : itérer
    seulement sur les jours ayant une activité produit un CTL qui ne redescend jamais."""

    def test_trou_de_dates_comble_par_zero(self) -> None:
        tss_by_date = {date(2026, 5, 19): 80.0, date(2026, 5, 22): 120.0}
        series = dense_daily_tss(tss_by_date, date(2026, 5, 19), date(2026, 5, 23))
        assert series == [80.0, 0.0, 0.0, 120.0, 0.0]

    def test_le_ctl_redescend_pendant_une_coupure(self) -> None:
        """End-to-end falsifiability: without zeros, last day's CTL would equal training
        day's instead of declining."""
        tss_by_date = {date(2026, 5, 19): 100.0}
        dense = dense_daily_tss(tss_by_date, date(2026, 5, 19), date(2026, 6, 18))
        assert len(dense) == 31
        days = run_pmc(dense, seed_ctl=0.0, seed_atl=0.0)
        assert days[-1].ctl < days[0].ctl
        assert days[-1].atl < days[0].atl
        # Counter-test: "activity-only days" series does not decline.
        sparse = run_pmc([100.0], seed_ctl=0.0, seed_atl=0.0)
        assert sparse[-1].ctl > days[-1].ctl

    def test_jour_unique(self) -> None:
        assert dense_daily_tss({}, date(2026, 5, 19), date(2026, 5, 19)) == [0.0]

    def test_dates_hors_intervalle_ignorees(self) -> None:
        tss_by_date = {date(2026, 5, 1): 999.0, date(2026, 5, 20): 50.0}
        assert dense_daily_tss(tss_by_date, date(2026, 5, 19), date(2026, 5, 20)) == [0.0, 50.0]

    def test_intervalle_inverse_refuse(self) -> None:
        with pytest.raises(ValueError, match="before"):
            dense_daily_tss({}, date(2026, 5, 20), date(2026, 5, 19))
