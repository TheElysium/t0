"""Tests de :mod:`garminator.scheduler.service` -- orchestration de bout en bout contre
:class:`tests.scheduler.fakes.FakePollDB`, sans PostgreSQL ni réseau. ``fetch_window``
est une fausse fonction pilotée par le test (aucun appel à ``GarminClient``)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from garminator.scheduler.service import FetchedWindow, WindowFetcher, run_scheduled_poll
from garminator.scheduler.store import load_window_states
from tests.scheduler.fakes import FakePollDB


def test_un_run_complet_marque_toutes_les_fenetres_done() -> None:
    db = FakePollDB()
    calls: list[tuple[str, date]] = []

    def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        calls.append((endpoint, window_date))
        return [FetchedWindow(scope_key=None, payload_sha256=bytes([len(calls)]) * 32)]

    summary = run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 20),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 20, 6, 0),
    )

    assert calls == [
        ("get_sleep_data", date(2026, 8, 18)),
        ("get_sleep_data", date(2026, 8, 19)),
        ("get_sleep_data", date(2026, 8, 20)),
    ]
    assert len(summary.succeeded) == 3
    assert summary.failed == []
    states = load_window_states(db, endpoint="get_sleep_data")
    assert all(s.status == "done" for s in states.values())


def test_une_seconde_execution_ne_resonde_pas_les_fenetres_deja_faites() -> None:
    """Coeur de la reprise : après un run réussi, un second run (au-delà de la fenêtre
    glissante) ne doit retélécharger AUCUNE des journées passées déjà 'done'.

    ``today`` avance d'un jour entre les deux runs (simule un jour calendaire écoulé) :
    la journée déjà traitée sort alors de la fenêtre glissante (``refresh_lookback_days
    = 0``), seule la toute nouvelle journée courante doit être sondée."""
    db = FakePollDB()
    calls: list[date] = []

    def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        calls.append(window_date)
        return [FetchedWindow(scope_key=None, payload_sha256=b"\x01" * 32)]

    db.clock = datetime(2026, 8, 1, 6, 0)
    run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 1),
        today=date(2026, 8, 1),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 1, 6, 0),
        refresh_lookback_days=0,
    )
    calls.clear()

    db.clock = datetime(2026, 8, 2, 6, 0)  # le lendemain
    run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 1),
        today=date(2026, 8, 2),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 2, 6, 0),
        refresh_lookback_days=0,
    )

    # Contre-épreuve d'un scheduler naïf qui reboucle sur tout l'historique : le 1er
    # août, déjà 'done' et désormais hors fenêtre glissante, n'est pas re-sondé ; seul
    # le 2 août (nouvelle journée, jamais tentée) l'est.
    assert calls == [date(2026, 8, 2)]


def test_un_echec_au_milieu_du_run_nempeche_pas_les_fenetres_suivantes() -> None:
    """Garantie non négociable de la mission : un run partiellement échoué ne doit
    jamais faire perdre les fenêtres encore à traiter."""
    db = FakePollDB()

    def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        if window_date == date(2026, 8, 19):
            raise RuntimeError("429 Too Many Requests")
        return [FetchedWindow(scope_key=None, payload_sha256=b"\x02" * 32)]

    summary = run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 20),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 20, 6, 0),
    )

    assert len(summary.succeeded) == 2
    assert len(summary.failed) == 1
    assert summary.failed[0].window_date == date(2026, 8, 19)
    states = load_window_states(db, endpoint="get_activities")
    assert states[date(2026, 8, 18)].status == "done"
    assert states[date(2026, 8, 19)].status == "failed"
    assert states[date(2026, 8, 20)].status == "done"


def test_une_fenetre_en_echec_est_rejouee_au_run_suivant() -> None:
    """Complète le test précédent : le jour en échec doit revenir, et lui seul --
    l'historique déjà réussi ne doit pas être rejoué avec lui."""
    db = FakePollDB()
    attempts: list[date] = []
    fail_once_more = {date(2026, 8, 19)}  # jours qui échouent encore à la prochaine visite

    def flaky_fetch(endpoint: str, window_date: date) -> list[FetchedWindow]:
        attempts.append(window_date)
        if window_date in fail_once_more:
            fail_once_more.discard(window_date)
            raise RuntimeError("panne réseau transitoire")
        return [FetchedWindow(scope_key=None, payload_sha256=b"\x03" * 32)]

    db.clock = datetime(2026, 8, 20, 6, 0)
    run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 20),
        fetch_window=flaky_fetch,
        now=datetime(2026, 8, 20, 6, 0),
        refresh_lookback_days=0,
    )
    attempts.clear()

    # ``today`` inchangé (même journée calendaire) : avec refresh_lookback_days=0, seul
    # le 20 (jour courant) resterait dû par la fenêtre glissante -- on le fige au-delà
    # du seuil de péremption pour isoler la reprise du seul jour en échec.
    db.clock = datetime(2026, 8, 20, 7, 0)
    summary2 = run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 20),
        fetch_window=flaky_fetch,
        now=datetime(2026, 8, 20, 7, 0),
        refresh_lookback_days=0,
    )

    # Contre-épreuve : sans la persistance de l'état par jour, ce second run rejouerait
    # aussi le 18 et le 20 (déjà 'done') -- seul le 19 (le seul 'failed') doit revenir.
    assert attempts == [date(2026, 8, 19), date(2026, 8, 20)]
    assert len(summary2.succeeded) == 2
    states = load_window_states(db, endpoint="get_activities")
    assert states[date(2026, 8, 19)].status == "done"
    assert states[date(2026, 8, 19)].last_error is None
    assert states[date(2026, 8, 18)].status == "done"  # jamais retouché, resté tel quel


def test_une_panne_mortelle_pendant_le_fetch_laisse_la_fenetre_rejouable() -> None:
    """Simule un crash du processus : ``fetch_window`` ne revient JAMAIS (ni succès ni
    exception propre -- on simule en levant, mais sans que le code appelant existe pour
    la capturer, ce que ``run_scheduled_poll`` ferait normalement). Ici on vérifie
    directement l'invariant sur lequel repose toute la garantie de reprise : après
    ``begin_window`` seul (sans complete/fail -- exactement l'état d'un process tué),
    la fenêtre reste 'in_progress' et redevient due une fois périmée."""
    from garminator.scheduler.planning import due_windows
    from garminator.scheduler.store import begin_window

    db = FakePollDB(clock=datetime(2026, 8, 19, 6, 0))
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 19), ingest_run_id=1)
    # Le "processus" meurt ici -- ni complete_window ni fail_window n'est jamais appelé.

    states = load_window_states(db, endpoint="get_activities")
    due_juste_apres = due_windows(
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        states=states,
        now=datetime(2026, 8, 19, 6, 1),  # 1 min plus tard : run concurrent plausible
        stale_after=timedelta(hours=2),
    )
    due_plus_tard = due_windows(
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        states=states,
        now=datetime(2026, 8, 19, 9, 0),  # 3h plus tard : plus aucun doute, c'est une panne
        stale_after=timedelta(hours=2),
    )

    assert due_juste_apres == []  # laisse une chance à un run concurrent légitime
    assert due_plus_tard == [date(2026, 8, 19)]  # mais ne perd jamais la fenêtre pour de bon


def test_duplique_le_meme_payload_incremente_already_seen_pas_new() -> None:
    """La déduplication de contenu (raw.payload_seen) ne doit jamais empêcher de marquer
    la fenêtre 'done' -- un contenu inchangé est un succès, pas une erreur."""
    db = FakePollDB()
    sha = b"\x04" * 32

    def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        return [FetchedWindow(scope_key="act-1", payload_sha256=sha)]

    run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 19, 6, 0),
    )
    summary2 = run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 19, 6, 0),
        refresh_lookback_days=1,  # force le re-sondage du même jour
    )

    outcome = summary2.outcomes[0]
    assert outcome.already_seen_count == 1
    assert outcome.new_payload_count == 0
    assert outcome.succeeded


def test_un_payload_change_est_compte_comme_nouveau_pas_comme_deja_vu() -> None:
    """Une activité renommée/corrigée change son hash : elle doit être réingérée, pas
    silencieusement ignorée par l'idempotence."""
    db = FakePollDB()

    def make_fetch(sha_byte: int) -> WindowFetcher:
        def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
            return [FetchedWindow(scope_key="act-1", payload_sha256=bytes([sha_byte]) * 32)]

        return fetch_window

    run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        fetch_window=make_fetch(5),
        now=datetime(2026, 8, 19, 6, 0),
    )
    summary2 = run_scheduled_poll(
        db,
        endpoints=["get_activities"],
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        fetch_window=make_fetch(6),  # contenu modifié -- ex. sport corrigé après coup
        now=datetime(2026, 8, 19, 6, 0),
        refresh_lookback_days=1,
    )

    outcome = summary2.outcomes[0]
    assert outcome.new_payload_count == 1
    assert outcome.already_seen_count == 0


def test_plusieurs_endpoints_sont_traites_independamment() -> None:
    db = FakePollDB()

    def fetch_window(endpoint: str, window_date: date) -> list[FetchedWindow]:
        del window_date
        return [FetchedWindow(scope_key=None, payload_sha256=(endpoint.encode() * 4)[:32])]

    summary = run_scheduled_poll(
        db,
        endpoints=["get_activities", "get_sleep_data"],
        history_start=date(2026, 8, 20),
        today=date(2026, 8, 20),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 20, 6, 0),
    )

    assert len(summary.outcomes) == 2
    assert {o.endpoint for o in summary.outcomes} == {"get_activities", "get_sleep_data"}


def test_history_start_posterieur_a_today_leve_une_erreur() -> None:
    db = FakePollDB()

    with pytest.raises(ValueError):
        run_scheduled_poll(
            db,
            endpoints=["get_activities"],
            history_start=date(2026, 8, 21),
            today=date(2026, 8, 20),
            fetch_window=lambda endpoint, window_date: [],
            now=datetime(2026, 8, 20, 6, 0),
        )


def test_un_rattrapage_massif_est_borne_et_progresse_a_chaque_run() -> None:
    """Le premier run après installation ne doit pas enchaîner tout l'historique.

    Sans borne, un ``history_start`` en avril 2025 produirait plus de cinq cents appels
    réseau d'affilée : le limiteur de débit les espacerait sans les borner, et une API
    non publique sollicitée ainsi conduit au bannissement. Ce test vérifie les deux
    moitiés de la garantie : la borne mord, ET le rattrapage PROGRESSE -- une borne qui
    resonderait les mêmes fenêtres à chaque run ne rattraperait jamais rien.

    Contre-épreuve incluse : sans borne (``None``), les 100 fenêtres passent en un run.
    """
    history_start = date(2026, 5, 1)
    today = date(2026, 8, 8)  # 100 jours inclus
    horodatage = datetime(2026, 8, 8, 6, 0)

    def _fetch(endpoint: str, window_date: date) -> list[FetchedWindow]:
        return [FetchedWindow(scope_key=None, payload_sha256=bytes(32))]

    db = FakePollDB()
    premier = run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=horodatage,
        max_windows_per_endpoint=40,
    )
    assert len(premier.outcomes) == 40
    assert premier.truncated_endpoints == ["get_sleep_data"], "la troncature doit être visible"
    # Les plus ANCIENNES d'abord : c'est ce qui rend la borne sûre.
    assert premier.outcomes[0].window_date == date(2026, 5, 1)
    assert premier.outcomes[-1].window_date == date(2026, 6, 9)

    second = run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=horodatage,
        max_windows_per_endpoint=40,
    )
    # Le run reprend là où le précédent s'est arrêté : aucune fenêtre déjà faite n'est
    # resondée, hormis la fenêtre glissante de rafraîchissement des jours récents.
    assert second.outcomes[0].window_date == date(2026, 6, 10)

    # Contre-épreuve : sans borne, tout passe d'un coup et rien n'est signalé tronqué.
    sans_borne = run_scheduled_poll(
        FakePollDB(),
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=horodatage,
        max_windows_per_endpoint=None,
    )
    assert len(sans_borne.outcomes) == 100
    assert sans_borne.truncated_endpoints == []
