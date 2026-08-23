"""Tests de :mod:`garminator.garmin.raw_store`, contre une connexion DB en mémoire.

Ces tests ne touchent aucun PostgreSQL réel : ``FakeConnection``/``FakeCursor``
(``tests/garmin/fakes.py``) implémentent seulement la vue structurelle
``garminator.garmin.raw_store.DBConnection`` dont ce module a besoin.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from garminator.garmin.raw_store import (
    canonical_json_dumps,
    finish_ingest_run,
    insert_api_response,
    payload_sha256,
    start_ingest_run,
)
from tests.garmin.fakes import FakeConnection


def test_canonical_json_dumps_sorts_keys_deterministically() -> None:
    a = canonical_json_dumps({"b": 1, "a": 2})
    b = canonical_json_dumps({"a": 2, "b": 1})

    assert a == b == '{"a":2,"b":1}'


def test_payload_sha256_matches_manual_hash_of_canonical_form() -> None:
    payload = {"z": [1, 2, 3], "a": "x"}

    expected = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).digest()

    assert payload_sha256(payload) == expected
    assert len(payload_sha256(payload)) == 32  # contrainte api_response_sha_len


def test_insert_api_response_writes_expected_columns_and_commits() -> None:
    conn = FakeConnection()

    response_id = insert_api_response(
        conn,
        endpoint="get_sleep_data",
        payload={"sleepScore": 82},
        scope_date=date(2026, 8, 18),
        scope_key=None,
        ingest_run_id=7,
    )

    assert response_id == 101  # FakeConnection démarre à 100, incrémente avant de retourner
    assert conn.commit_count == 1
    query, params = conn.executed[0]
    assert "INSERT INTO raw.api_response" in query
    assert "RETURNING id" in query
    endpoint, scope_date, scope_key, http_status, payload, sha, ingest_run_id = params
    assert endpoint == "get_sleep_data"
    assert scope_date == date(2026, 8, 18)
    assert scope_key is None
    assert http_status == 200
    assert payload.obj == {"sleepScore": 82}  # Jsonb expose .obj
    assert len(sha) == 32
    assert ingest_run_id == 7


def test_insert_api_response_defaults_http_status_to_200() -> None:
    conn = FakeConnection()

    insert_api_response(conn, endpoint="get_activities", payload=[])

    _query, params = conn.executed[0]
    assert params[3] == 200


def test_start_and_finish_ingest_run_roundtrip() -> None:
    conn = FakeConnection()

    run_id = start_ingest_run(conn, mode="daily", code_version="abc123")
    assert run_id == 1
    query, params = conn.executed[0]
    assert "INSERT INTO raw.ingest_run" in query
    assert params == ("daily", "abc123")

    finish_ingest_run(conn, ingest_run_id=run_id, status="ok", notes={"requests": 3})

    query2, params2 = conn.executed[1]
    assert "UPDATE raw.ingest_run" in query2
    status, notes, ingest_run_id = params2
    assert status == "ok"
    assert notes.obj == {"requests": 3}
    assert ingest_run_id == 1
    assert conn.commit_count == 2


def test_finish_ingest_run_accepts_no_notes() -> None:
    conn = FakeConnection()
    run_id = start_ingest_run(conn, mode="manual")

    finish_ingest_run(conn, ingest_run_id=run_id, status="failed", notes=None)

    _query, params = conn.executed[-1]
    assert params[1] is None


def test_json_module_actually_used_for_canonicalization() -> None:
    # Non-régression : s'assurer que canonical_json_dumps reste basé sur json.dumps
    # (et non une sérialisation maison qui diverge sur les types Python non triviaux).
    payload = {"when": date(2026, 8, 19)}
    assert json.loads(canonical_json_dumps(payload)) == {"when": "2026-08-19"}
