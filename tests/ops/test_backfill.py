"""Tests de :mod:`garminator.ops.backfill` contre
:class:`tests.scheduler.fakes.FakePollDB`, sans PostgreSQL ni réseau -- même doublure
que ``tests/scheduler/test_service.py``, réutilisée telle quelle (convention du dépôt :
cf. son import dans ``tests/scheduler/test_store.py``)."""

from __future__ import annotations

from datetime import date, datetime

from garminator.ops.backfill import backfill_progress
from garminator.scheduler.store import begin_window, complete_window, fail_window
from tests.scheduler.fakes import FakePollDB


def test_une_base_vierge_a_toutes_les_fenetres_dues_et_jamais_tentees() -> None:
    db = FakePollDB()
    db.clock = datetime(2026, 8, 21, 6, 0)

    progress = backfill_progress(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 21),
    )

    assert progress.total_due == 4
    assert progress.fully_caught_up is False
    endpoint_progress = progress.by_endpoint[0]
    assert endpoint_progress.total_days == 4
    assert endpoint_progress.done == 0
    assert endpoint_progress.never_attempted == 4
    assert endpoint_progress.failed == 0
    assert endpoint_progress.stale_in_progress == 0
    assert endpoint_progress.oldest_due == date(2026, 8, 18)
    assert endpoint_progress.is_caught_up is False


def test_un_rattrapage_complet_ne_laisse_plus_aucune_fenetre_due() -> None:
    """``due_windows`` resonde TOUJOURS la fenêtre du jour courant (day >= refresh_floor
    est vrai pour ``day == today`` quel que soit ``refresh_lookback_days`` >= 0) : une
    fenêtre 'done' pour aujourd'hui ne compte donc comme non due que si elle est
    ``in_progress`` récente (sondage en cours), pas ``done`` -- c'est le seul état qui
    représente un « rattrapage complet, rien à faire de plus MAINTENANT »."""
    db = FakePollDB()
    db.clock = datetime(2026, 8, 21, 6, 0)
    for day in (18, 19, 20):
        window_date = date(2026, 8, day)
        begin_window(db, endpoint="get_sleep_data", window_date=window_date, ingest_run_id=None)
        complete_window(db, endpoint="get_sleep_data", window_date=window_date)
    # Le sondage du jour courant est en cours, tout juste démarré : ni en échec, ni
    # périmé -- due_windows ne le propose donc pas une seconde fois.
    begin_window(db, endpoint="get_sleep_data", window_date=date(2026, 8, 21), ingest_run_id=None)

    progress = backfill_progress(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 21),
        now=db.clock,
        # Aucune fenêtre glissante : les fenêtres 'done' du passé ne redeviennent pas dues.
        refresh_lookback_days=0,
    )

    assert progress.total_due == 0
    assert progress.fully_caught_up is True
    assert progress.by_endpoint[0].done == 3
    assert progress.by_endpoint[0].oldest_due is None


def test_une_fenetre_en_echec_est_comptee_a_la_fois_dans_due_et_dans_failed() -> None:
    db = FakePollDB()
    db.clock = datetime(2026, 8, 21, 6, 0)
    window_date = date(2026, 8, 18)
    begin_window(db, endpoint="get_sleep_data", window_date=window_date, ingest_run_id=None)
    fail_window(db, endpoint="get_sleep_data", window_date=window_date, error="500")

    progress = backfill_progress(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 18),
    )

    endpoint_progress = progress.by_endpoint[0]
    assert endpoint_progress.due == 1
    assert endpoint_progress.failed == 1
    assert endpoint_progress.never_attempted == 0


def test_plusieurs_endpoints_sont_rapportes_independamment() -> None:
    """L'avancement se calcule par endpoint : le rattrapage de l'un ne masque pas le
    retard de l'autre. Le sondage de ``get_sleep_data`` est ``in_progress`` frais et non
    ``done``, pour la raison documentée sur
    :func:`test_un_rattrapage_complet_ne_laisse_plus_aucune_fenetre_due` -- une fenêtre
    'done' pour le jour courant reste due, quel que soit ``refresh_lookback_days``."""
    db = FakePollDB()
    db.clock = datetime(2026, 8, 21, 6, 0)
    begin_window(db, endpoint="get_sleep_data", window_date=date(2026, 8, 18), ingest_run_id=None)

    progress = backfill_progress(
        db,
        endpoints=["get_sleep_data", "get_activities"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 18),
        now=db.clock,
        refresh_lookback_days=0,
    )

    by_endpoint = {p.endpoint: p for p in progress.by_endpoint}
    assert by_endpoint["get_sleep_data"].is_caught_up is True
    assert by_endpoint["get_activities"].is_caught_up is False
    assert by_endpoint["get_activities"].never_attempted == 1
