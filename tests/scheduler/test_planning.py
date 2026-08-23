"""Tests de :mod:`garminator.scheduler.planning` -- purs, sans DB ni réseau."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from garminator.scheduler.planning import WindowState, daily_range, due_windows


def test_daily_range_est_inclusif_aux_deux_bornes() -> None:
    days = daily_range(date(2026, 8, 1), date(2026, 8, 3))

    assert days == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


def test_daily_range_une_seule_journee() -> None:
    assert daily_range(date(2026, 8, 1), date(2026, 8, 1)) == [date(2026, 8, 1)]


def test_daily_range_refuse_un_intervalle_inverse() -> None:
    # Contre-épreuve : sans cette garde, une inversion silencieuse renverrait []
    # plutôt que de signaler l'erreur d'appel.
    with pytest.raises(ValueError, match="doit être <="):
        daily_range(date(2026, 8, 5), date(2026, 8, 1))


class TestDueWindowsHistorique:
    def test_une_fenetre_jamais_tentee_est_due(self) -> None:
        due = due_windows(
            history_start=date(2026, 8, 18),
            today=date(2026, 8, 18),
            states={},
            now=datetime(2026, 8, 18, 6, 0),
        )

        assert due == [date(2026, 8, 18)]

    def test_une_fenetre_done_hors_fenetre_glissante_nest_pas_due(self) -> None:
        """Falsifiabilité : sans le filtre de fraîcheur, cette fenêtre 'done' ancienne
        serait re-proposée à chaque run, ce qui retéléchargerait tout l'historique."""
        states = {
            date(2026, 8, 1): WindowState(
                status="done", attempts=1, last_attempted_at=None, last_error=None
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 1),
            today=date(2026, 8, 10),
            states=states,
            now=datetime(2026, 8, 10, 6, 0),
            refresh_lookback_days=2,
        )

        assert date(2026, 8, 1) not in due

    def test_une_fenetre_done_dans_la_fenetre_glissante_reste_due(self) -> None:
        """Rattrape une republication tardive de Garmin (activité renommée, corrigée)."""
        states = {
            date(2026, 8, 9): WindowState(
                status="done", attempts=1, last_attempted_at=None, last_error=None
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 9),
            today=date(2026, 8, 10),
            states=states,
            now=datetime(2026, 8, 10, 6, 0),
            refresh_lookback_days=2,
        )

        assert date(2026, 8, 9) in due
        assert date(2026, 8, 10) in due  # jour courant, jamais tenté

    def test_une_fenetre_failed_reste_toujours_due(self) -> None:
        states = {
            date(2026, 8, 5): WindowState(
                status="failed", attempts=3, last_attempted_at=None, last_error="429"
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 5),
            today=date(2026, 8, 5),
            states=states,
            now=datetime(2026, 8, 5, 6, 0),
        )

        assert due == [date(2026, 8, 5)]


class TestDueWindowsPanne:
    def test_in_progress_recent_nest_pas_reconsidere_comme_du(self) -> None:
        """Une fenêtre en cours de traitement (run concurrent ou pas encore expiré) ne
        doit pas être reproposée -- sinon un même jour serait sondé deux fois en
        parallèle."""
        states = {
            date(2026, 8, 20): WindowState(
                status="in_progress",
                attempts=1,
                last_attempted_at=datetime(2026, 8, 20, 5, 59),
                last_error=None,
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 20),
            today=date(2026, 8, 20),
            states=states,
            now=datetime(2026, 8, 20, 6, 0),  # 1 minute plus tard
            stale_after=timedelta(hours=2),
        )

        assert due == []

    def test_in_progress_perime_est_rejoue_apres_une_panne(self) -> None:
        """Le coeur de la garantie de reprise : un run mort au milieu (in_progress figé)
        ne doit jamais faire perdre la fenêtre. Contre-épreuve : sans ce cas, la fenêtre
        resterait in_progress pour toujours, jamais rejouée -- exactement la perte
        interdite par la mission."""
        states = {
            date(2026, 8, 20): WindowState(
                status="in_progress",
                attempts=1,
                last_attempted_at=datetime(2026, 8, 20, 3, 0),
                last_error=None,
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 20),
            today=date(2026, 8, 20),
            states=states,
            now=datetime(2026, 8, 20, 6, 0),  # 3h plus tard
            stale_after=timedelta(hours=2),
        )

        assert due == [date(2026, 8, 20)]

    def test_in_progress_sans_horodatage_est_toujours_considere_perime(self) -> None:
        """Cas défensif : une ligne in_progress sans last_attempted_at (ne devrait pas
        arriver via begin_window, mais ne doit jamais bloquer silencieusement)."""
        states = {
            date(2026, 8, 20): WindowState(
                status="in_progress", attempts=1, last_attempted_at=None, last_error=None
            )
        }

        due = due_windows(
            history_start=date(2026, 8, 20),
            today=date(2026, 8, 20),
            states=states,
            now=datetime(2026, 8, 20, 6, 0),
        )

        assert due == [date(2026, 8, 20)]


def test_due_windows_ordre_chronologique_croissant() -> None:
    """Rattrapage d'historique : les plus vieux trous d'abord, jamais le jour courant en
    boucle tant que le passé n'est pas comblé."""
    due = due_windows(
        history_start=date(2026, 8, 1),
        today=date(2026, 8, 3),
        states={},
        now=datetime(2026, 8, 3, 6, 0),
    )

    assert due == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


def test_due_windows_refuse_un_lookback_negatif() -> None:
    with pytest.raises(ValueError, match="refresh_lookback_days"):
        due_windows(
            history_start=date(2026, 8, 1),
            today=date(2026, 8, 1),
            states={},
            now=datetime(2026, 8, 1, 6, 0),
            refresh_lookback_days=-1,
        )
