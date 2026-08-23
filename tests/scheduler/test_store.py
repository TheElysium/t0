"""Tests de :mod:`garminator.scheduler.store`, contre une connexion DB en mémoire
(:class:`tests.scheduler.fakes.FakePollDB`) -- aucun PostgreSQL réel."""

from __future__ import annotations

from datetime import date

from garminator.scheduler.planning import WindowState
from garminator.scheduler.store import (
    begin_window,
    complete_window,
    fail_window,
    has_payload_been_seen,
    load_window_states,
    record_payload_seen,
)
from tests.scheduler.fakes import FakePollDB


def test_load_window_states_est_vide_sans_ligne() -> None:
    db = FakePollDB()

    assert load_window_states(db, endpoint="get_activities") == {}


def test_begin_window_cree_une_ligne_in_progress_et_incremente_attempts() -> None:
    db = FakePollDB()

    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=7)
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=8)

    states = load_window_states(db, endpoint="get_activities")
    state = states[date(2026, 8, 20)]
    assert state.status == "in_progress"
    assert state.attempts == 2  # deuxième tentative -- pas remis à zéro
    assert db.commit_count == 2


def test_begin_window_est_isole_par_endpoint() -> None:
    db = FakePollDB()

    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)

    assert load_window_states(db, endpoint="get_sleep_data") == {}


def test_complete_window_marque_done_et_efface_la_derniere_erreur() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)
    fail_window(
        db, endpoint="get_activities", window_date=date(2026, 8, 20), error="429 rate limit"
    )

    complete_window(db, endpoint="get_activities", window_date=date(2026, 8, 20))

    state = load_window_states(db, endpoint="get_activities")[date(2026, 8, 20)]
    assert state.status == "done"
    # Contre-épreuve : sans le SET last_error = NULL, l'erreur du run précédent
    # resterait affichée à côté d'un succès plus récent.
    assert state.last_error is None


def test_fail_window_marque_failed_avec_le_message() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)

    fail_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), error="429")

    state = load_window_states(db, endpoint="get_activities")[date(2026, 8, 20)]
    assert state.status == "failed"
    assert state.last_error == "429"


def test_has_payload_been_seen_faux_avant_tout_enregistrement() -> None:
    db = FakePollDB()

    assert not has_payload_been_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=b"\x00" * 32,
    )


def test_record_puis_has_payload_been_seen_devient_vrai() -> None:
    db = FakePollDB()
    sha = b"\x01" * 32

    record_payload_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=sha,
    )

    assert has_payload_been_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=sha,
    )


def test_has_payload_been_seen_discrimine_par_hash() -> None:
    """Falsifiabilité : un contenu différent (renommage, sport corrigé) ne doit jamais
    être confondu avec l'ancien -- c'est ce qui permet la réingestion légitime."""
    db = FakePollDB()
    record_payload_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=b"\x01" * 32,
    )

    assert not has_payload_been_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=b"\x02" * 32,
    )


def test_record_payload_seen_est_idempotent_sans_erreur_de_doublon() -> None:
    db = FakePollDB()
    sha = b"\x03" * 32

    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=sha
    )
    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=sha
    )

    key = ("e", date(2026, 8, 20), "", sha)  # scope_key=None normalisé en '' (défaut DDL)
    assert db.payload_seen[key] == 2  # compteur de republications, pas de doublon de ligne


def test_scope_key_none_est_normalise_en_chaine_vide() -> None:
    """``raw.payload_seen.scope_key`` est NOT NULL (participe à la clé primaire) ; None
    doit être normalisé vers le même défaut '' que le DDL, jamais laissé tel quel."""
    db = FakePollDB()

    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=b"\x09" * 32
    )

    _query, params = db.executed[-1]
    assert params[2] == ""


def test_load_window_states_reflete_bien_le_type_publie() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="e", window_date=date(2026, 8, 20), ingest_run_id=None)

    state = load_window_states(db, endpoint="e")[date(2026, 8, 20)]

    assert isinstance(state, WindowState)
