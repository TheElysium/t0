"""Tests for :mod:`t0.garmin.client`, entirely on simulated transport and DB.

Covers three acceptance requirements for this module:
- existing tokens are reused without SSO (and restoration is logged);
- empty tokenstore triggers full SSO, logged explicitly (never silently);
- authentication failure triggers no retry.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from t0.garmin.client import GarminClient, ingest_run
from t0.garmin.rate_limit import RateLimiter
from t0.garmin.retry import RetryExhaustedError, RetryPolicy
from t0.garmin.tokenstore import TOKEN_FILENAME
from tests.garmin.fakes import FakeConnection, FakeTransport

if TYPE_CHECKING:
    from pathlib import Path


def _client(
    transport: FakeTransport,
    tokenstore: Path,
    conn: FakeConnection | None = None,
) -> GarminClient:
    return GarminClient(
        transport=transport,
        tokenstore_path=tokenstore,
        conn=conn or FakeConnection(),
        rate_limiter=RateLimiter(min_interval_s=0.0, sleep=lambda _s: None),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter_s=0.0),
        sleep=lambda _s: None,
    )


def test_login_reuses_existing_tokens_without_full_sso(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tokenstore = tmp_path / "tokens"
    tokenstore.mkdir()
    (tokenstore / TOKEN_FILENAME).write_text(
        json.dumps({"di_token": "already-there"}), encoding="utf-8"
    )

    transport = FakeTransport()
    client = _client(transport, tokenstore)

    with caplog.at_level(logging.INFO, logger="t0.garmin.client"):
        client.login()

    assert transport.did_full_sso is False
    assert transport.login_calls == [str(tokenstore)]
    assert any("restored" in record.message for record in caplog.records)
    assert not any(
        record.levelno == logging.WARNING and "Full Garmin SSO" in record.message
        for record in caplog.records
    )


def test_login_with_empty_tokenstore_performs_and_logs_full_sso(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tokenstore = tmp_path / "tokens"  # doesn't exist yet

    transport = FakeTransport()
    client = _client(transport, tokenstore)

    with caplog.at_level(logging.INFO, logger="t0.garmin.client"):
        client.login()

    assert transport.did_full_sso is True
    assert any(
        record.levelno == logging.WARNING and "Full Garmin SSO" in record.message
        for record in caplog.records
    )


def test_authentication_failure_during_login_is_not_retried(tmp_path: Path) -> None:
    tokenstore = tmp_path / "tokens"
    transport = FakeTransport(
        login_outcomes=[GarminConnectAuthenticationError("invalid credentials")]
    )
    client = _client(transport, tokenstore)

    with pytest.raises(GarminConnectAuthenticationError):
        client.login()

    assert len(transport.login_calls) == 1  # no retry


def test_fetch_429_then_429_then_success_writes_raw_api_response(tmp_path: Path) -> None:
    transport = FakeTransport(
        queued_outcomes={
            "get_sleep_data": [
                GarminConnectTooManyRequestsError("429"),
                GarminConnectTooManyRequestsError("429"),
                {"sleepScore": 91},
            ]
        }
    )
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    result = client.fetch("get_sleep_data", "2026-08-18")

    assert result == {"sleepScore": 91}
    assert transport.call_counts["get_sleep_data"] == 3
    insert_query, params = conn.executed[0]
    assert "INSERT INTO raw.api_response" in insert_query
    assert params[0] == "get_sleep_data"
    assert params[4].obj == {"sleepScore": 91}


def test_fetch_authentication_error_is_not_retried_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        queued_outcomes={"get_activities": [GarminConnectAuthenticationError("nope")]}
    )
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    with pytest.raises(GarminConnectAuthenticationError):
        client.fetch("get_activities")

    assert transport.call_counts["get_activities"] == 1
    assert conn.executed == []  # nothing written to raw.api_response


def test_fetch_exhausts_retries_and_raises_retry_exhausted_error(tmp_path: Path) -> None:
    transport = FakeTransport(
        queued_outcomes={
            "get_stress_data": [
                GarminConnectTooManyRequestsError("429"),
                GarminConnectTooManyRequestsError("429"),
                GarminConnectTooManyRequestsError("429"),
            ]
        }
    )
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    with pytest.raises(RetryExhaustedError):
        client.fetch("get_stress_data")

    assert transport.call_counts["get_stress_data"] == 3  # = max_attempts de _client
    assert conn.executed == []


def test_fetch_records_scope_date_and_scope_key(tmp_path: Path) -> None:
    from datetime import date

    transport = FakeTransport(responses={"get_activity": {"activityId": 42}})
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    client.fetch(
        "get_activity",
        "42",
        scope_date=date(2026, 8, 18),
        scope_key="42",
    )

    _query, params = conn.executed[0]
    assert params[1] == date(2026, 8, 18)
    assert params[2] == "42"


def test_ingest_run_tracker_records_success_and_marks_ok(tmp_path: Path) -> None:
    transport = FakeTransport(responses={"get_activities": []})
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    with ingest_run(conn, mode="daily", code_version="v1") as tracker:
        client.fetch("get_activities", tracker=tracker)

    assert tracker.requests == 1
    assert tracker.errors == 0
    update_query, params = conn.executed[-1]
    assert "UPDATE raw.ingest_run" in update_query
    status, notes, _run_id = params
    assert status == "ok"
    assert notes.obj == {"requests": 1, "errors": 0}


def test_ingest_run_tracker_records_error_and_marks_partial(tmp_path: Path) -> None:
    transport = FakeTransport(
        queued_outcomes={"get_activities": [GarminConnectAuthenticationError("nope")]}
    )
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    # Important order: ``pytest.raises`` must be the outermost context so ``ingest_run``
    # (innermost) sees the exception first on exit and closes with "failed" before
    # pytest.raises swallows it.
    with pytest.raises(GarminConnectAuthenticationError), ingest_run(conn, mode="daily") as tracker:
        client.fetch("get_activities", tracker=tracker)

    assert tracker.requests == 1
    assert tracker.errors == 1
    # Exception crossed the ingest_run block without being caught -> closes as
    # "failed" (not "partial"), with error message recorded in ``notes``.
    update_query, params = conn.executed[-1]
    status, notes, _run_id = params
    assert status == "failed"
    assert notes.obj["error"]


def test_ingest_run_tracker_marks_partial_when_caller_absorbs_the_error(tmp_path: Path) -> None:
    transport = FakeTransport(
        queued_outcomes={"get_activities": [GarminConnectAuthenticationError("nope")]}
    )
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    with (
        ingest_run(conn, mode="daily") as tracker,
        contextlib.suppress(GarminConnectAuthenticationError),
    ):
        # Caller chooses to absorb the error and continue despite it.
        client.fetch("get_activities", tracker=tracker)

    assert tracker.errors == 1
    update_query, params = conn.executed[-1]
    status, _notes, _run_id = params
    assert status == "partial"


def test_download_original_returns_bytes_and_real_filename(tmp_path: Path) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("55555.tcx", b"<TrainingCenterDatabase/>")

    transport = FakeTransport(download_outcomes=[buf.getvalue()])
    conn = FakeConnection()
    client = _client(transport, tmp_path / "tokens", conn)

    result = client.download_original("55555", dl_fmt="ORIGINAL")

    assert result.filename == "55555.tcx"
    assert result.content == buf.getvalue()
    assert result.activity_id == "55555"
    assert conn.executed == []  # no raw.api_response write for binary


def test_download_original_429_then_success_is_retried(tmp_path: Path) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1.fit", b"fake fit")

    transport = FakeTransport(
        download_outcomes=[GarminConnectTooManyRequestsError("429"), buf.getvalue()]
    )
    client = _client(transport, tmp_path / "tokens")

    result = client.download_original("1", dl_fmt="ORIGINAL")

    assert result.filename == "1.fit"
    assert len(transport.download_calls) == 2


# --- login(): never retry, reliable full SSO detection ---------------------------------
#
# ``Garmin.login()`` converts SSO 429 to ``GarminConnectTooManyRequestsError`` and
# *any other* failure to ``GarminConnectConnectionError`` (installed source,
# ``garminconnect/__init__.py`` lines ~560-579). Both types are retryable on the data
# path; retrying them on login would chain up to ``max_attempts`` full SSOs seconds
# apart — the loop that locks out an account. So ``login()`` must make only one call,
# no matter what.


@pytest.mark.parametrize(
    "failure",
    [
        GarminConnectTooManyRequestsError("Too many login attempts"),
        GarminConnectConnectionError("Login failed: boom"),
        GarminConnectAuthenticationError("invalid credentials"),
    ],
)
def test_login_never_retries_whatever_the_failure(tmp_path: Path, failure: Exception) -> None:
    transport = FakeTransport(login_outcomes=[failure])
    slept: list[float] = []
    client = _client(transport, tmp_path / "tokens")
    client.sleep = slept.append

    with pytest.raises(type(failure)):
        client.login()

    assert len(transport.login_calls) == 1
    assert slept == []


def test_login_warns_when_garminconnect_rewrites_the_tokens(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tokens present but expired: garminconnect redoes SSO silently.

    The wrapper can only learn this by comparing token file fingerprint before/after:
    ``garminconnect`` rewrites ``garmin_tokens.json`` only during full SSO or refresh.
    We simulate the dangerous case here — tokenstore appears populated yet tokens are
    rewritten.
    """
    tokenstore = tmp_path / "tokens"
    tokenstore.mkdir()
    tokens = tokenstore / TOKEN_FILENAME
    tokens.write_text(json.dumps({"di_token": "expired"}), encoding="utf-8")

    class RewritingTransport(FakeTransport):
        def login(self, tokenstore: str) -> tuple[str | None, str | None]:
            self.login_calls.append(tokenstore)
            # What garminconnect does when reloading fails: SSO then dump().
            tokens.write_text(json.dumps({"di_token": "tout-neuf"}), encoding="utf-8")
            return (None, None)

    client = _client(RewritingTransport(), tokenstore)

    with caplog.at_level(logging.INFO, logger="t0.garmin.client"):
        client.login()

    assert any(
        record.levelno == logging.WARNING and "REWRITTEN" in record.message
        for record in caplog.records
    ), "silent SSO must be flagged, not hidden by 'session restored'"


def test_login_does_not_warn_when_tokens_are_untouched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tokenstore = tmp_path / "tokens"
    tokenstore.mkdir()
    (tokenstore / TOKEN_FILENAME).write_text(json.dumps({"di_token": "ok"}), encoding="utf-8")

    client = _client(FakeTransport(), tokenstore)

    with caplog.at_level(logging.INFO, logger="t0.garmin.client"):
        client.login()

    assert not any("REWRITTEN" in record.message for record in caplog.records)
