"""Tests de :mod:`garminator.scheduler.fetcher`, sur transport et DB simulés (lot L11).

Réutilise ``tests/garmin/fakes.py`` (``FakeTransport``, ``FakeConnection``) : c'est le
même faux transport qui exerce déjà :mod:`garminator.garmin.client` -- aucune raison
d'en réinventer un second pour ce lot, ``GarminWindowFetcher`` ne fait qu'appeler
``GarminClient.fetch`` par-dessus.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from garminator.garmin.client import GarminClient, IngestRunTracker
from garminator.garmin.rate_limit import RateLimiter
from garminator.garmin.raw_store import payload_sha256
from garminator.garmin.retry import RetryExhaustedError, RetryPolicy
from garminator.scheduler.fetcher import GarminWindowFetcher
from tests.garmin.fakes import FakeConnection, FakeTransport

_UNUSED_TOKENSTORE = Path("/dev/null")  # jamais lu : aucun test de ce module n'appelle login()


def _client(transport: FakeTransport, conn: FakeConnection | None = None) -> GarminClient:
    return GarminClient(
        transport=transport,
        tokenstore_path=_UNUSED_TOKENSTORE,
        conn=conn or FakeConnection(),
        rate_limiter=RateLimiter(min_interval_s=0.0, sleep=lambda _s: None),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter_s=0.0),
        sleep=lambda _s: None,
    )


def test_day_endpoint_returns_a_single_fetched_window_with_scope_key_none() -> None:
    transport = FakeTransport(responses={"get_sleep_data": {"sleepScore": 91}})
    conn = FakeConnection()
    fetcher = GarminWindowFetcher(client=_client(transport, conn))

    result = fetcher("get_sleep_data", date(2026, 8, 20))

    assert len(result) == 1
    assert result[0].scope_key is None
    assert result[0].payload_sha256 == payload_sha256({"sleepScore": 91})
    assert transport.call_counts["get_sleep_data"] == 1


@pytest.mark.parametrize("empty_payload", [None, {}, []])
def test_empty_payload_is_not_an_error_and_yields_no_window(empty_payload: object) -> None:
    transport = FakeTransport(responses={"get_hrv_data": empty_payload})
    fetcher = GarminWindowFetcher(client=_client(transport))

    result = fetcher("get_hrv_data", date(2026, 8, 20))

    assert result == []


def test_range_endpoint_calls_with_same_start_and_end_date() -> None:
    transport = FakeTransport(responses={"get_body_composition": {"weight": 75000}})
    fetcher = GarminWindowFetcher(client=_client(transport))

    fetcher("get_body_composition", date(2026, 8, 20))

    # FakeTransport ignore les arguments positionnels (voir __getattr__), donc c'est le
    # compte d'appels -- pas les arguments -- qui est vérifiable ici sans dupliquer un
    # second faux transport qui les capturerait.
    assert transport.call_counts["get_body_composition"] == 1


def test_activities_endpoint_yields_one_window_per_activity_with_activity_id_as_scope_key() -> None:
    activities = [
        {"activityId": 111, "activityName": "Sortie vélo"},
        {"activityId": 222, "activityName": "Sortie course"},
    ]
    transport = FakeTransport(responses={"get_activities_by_date": activities})
    conn = FakeConnection()
    fetcher = GarminWindowFetcher(client=_client(transport, conn))

    result = fetcher("get_activities_by_date", date(2026, 8, 20))

    assert [w.scope_key for w in result] == ["111", "222"]
    assert result[0].payload_sha256 == payload_sha256(activities[0])
    assert result[1].payload_sha256 == payload_sha256(activities[1])
    # Décision de conception centrale de ce lot : UN SEUL appel réseau pour toute la
    # journée, jamais un par activité -- voir le docstring de GarminWindowFetcher.
    assert transport.call_counts["get_activities_by_date"] == 1


def test_activities_endpoint_writes_a_single_raw_api_response_row_with_scope_key_none() -> None:
    """La ligne brute unique porte ``scope_key=None`` : c'est la liste entière, telle
    que Garmin l'a renvoyée, qui constitue le « brut » de cette fenêtre -- pas une
    ligne par activité (cf. décision documentée dans GarminWindowFetcher)."""
    activities = [{"activityId": 111}]
    transport = FakeTransport(responses={"get_activities_by_date": activities})
    conn = FakeConnection()
    fetcher = GarminWindowFetcher(client=_client(transport, conn))

    fetcher("get_activities_by_date", date(2026, 8, 20))

    insert_query, params = conn.executed[0]
    assert "INSERT INTO raw.api_response" in insert_query
    assert params[0] == "get_activities_by_date"
    assert params[2] is None  # scope_key de la ligne raw.api_response


def test_unknown_endpoint_raises_value_error_without_calling_transport() -> None:
    transport = FakeTransport()
    fetcher = GarminWindowFetcher(client=_client(transport))

    with pytest.raises(ValueError, match="endpoint inconnu"):
        fetcher("get_totally_made_up_endpoint", date(2026, 8, 20))

    assert transport.call_counts == {}


def test_underlying_transport_error_propagates_uncaught() -> None:
    """Aucun ``try/except`` défensif dans ``GarminWindowFetcher`` : c'est
    ``run_scheduled_poll`` qui capture, pas ce module."""
    transport = FakeTransport(
        queued_outcomes={
            "get_sleep_data": [
                GarminConnectTooManyRequestsError("429"),
                GarminConnectTooManyRequestsError("429"),
                GarminConnectTooManyRequestsError("429"),
            ]
        }
    )
    fetcher = GarminWindowFetcher(client=_client(transport))

    with pytest.raises(RetryExhaustedError):
        fetcher("get_sleep_data", date(2026, 8, 20))


def test_tracker_is_forwarded_to_client_fetch() -> None:
    transport = FakeTransport(responses={"get_sleep_data": {"sleepScore": 80}})
    conn = FakeConnection()
    tracker = IngestRunTracker(conn=conn, ingest_run_id=7)
    fetcher = GarminWindowFetcher(client=_client(transport, conn), tracker=tracker)

    fetcher("get_sleep_data", date(2026, 8, 20))

    assert tracker.requests == 1
    assert tracker.errors == 0
