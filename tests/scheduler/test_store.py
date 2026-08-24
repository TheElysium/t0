"""Tests for :mod:`garminator.scheduler.store`, against an in-memory DB connection
(:class:`tests.scheduler.fakes.FakePollDB`) -- no real PostgreSQL."""

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


def test_load_window_states_empty_without_rows() -> None:
    db = FakePollDB()

    assert load_window_states(db, endpoint="get_activities") == {}


def test_begin_window_creates_in_progress_row_and_increments_attempts() -> None:
    db = FakePollDB()

    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=7)
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=8)

    states = load_window_states(db, endpoint="get_activities")
    state = states[date(2026, 8, 20)]
    assert state.status == "in_progress"
    assert state.attempts == 2  # second attempt -- not reset to zero
    assert db.commit_count == 2


def test_begin_window_is_isolated_by_endpoint() -> None:
    db = FakePollDB()

    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)

    assert load_window_states(db, endpoint="get_sleep_data") == {}


def test_complete_window_marks_done_and_clears_last_error() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)
    fail_window(
        db, endpoint="get_activities", window_date=date(2026, 8, 20), error="429 rate limit"
    )

    complete_window(db, endpoint="get_activities", window_date=date(2026, 8, 20))

    state = load_window_states(db, endpoint="get_activities")[date(2026, 8, 20)]
    assert state.status == "done"
    # Sanity check: without SET last_error = NULL, the error from previous run
    # would still show alongside a more recent success.
    assert state.last_error is None


def test_fail_window_marks_failed_with_message() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), ingest_run_id=1)

    fail_window(db, endpoint="get_activities", window_date=date(2026, 8, 20), error="429")

    state = load_window_states(db, endpoint="get_activities")[date(2026, 8, 20)]
    assert state.status == "failed"
    assert state.last_error == "429"


def test_has_payload_been_seen_false_before_recording() -> None:
    db = FakePollDB()

    assert not has_payload_been_seen(
        db,
        endpoint="get_activities",
        scope_date=date(2026, 8, 20),
        scope_key="12345",
        payload_sha256=b"\x00" * 32,
    )


def test_record_then_has_payload_been_seen_becomes_true() -> None:
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


def test_has_payload_been_seen_discriminates_by_hash() -> None:
    """Falsifiability: different content (rename, sport corrected) must never
    be confused with the old one -- this is what allows legitimate reingestion."""
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


def test_record_payload_seen_is_idempotent_without_duplicate_error() -> None:
    db = FakePollDB()
    sha = b"\x03" * 32

    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=sha
    )
    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=sha
    )

    key = ("e", date(2026, 8, 20), "", sha)  # scope_key=None normalized to '' (DDL default)
    assert db.payload_seen[key] == 2  # republication counter, not row duplicate


def test_scope_key_none_is_normalized_to_empty_string() -> None:
    """``raw.payload_seen.scope_key`` is NOT NULL (part of primary key) ; None
    must be normalized to the same default '' as DDL, never left as-is."""
    db = FakePollDB()

    record_payload_seen(
        db, endpoint="e", scope_date=date(2026, 8, 20), scope_key=None, payload_sha256=b"\x09" * 32
    )

    _query, params = db.executed[-1]
    assert params[2] == ""


def test_load_window_states_reflects_published_type() -> None:
    db = FakePollDB()
    begin_window(db, endpoint="e", window_date=date(2026, 8, 20), ingest_run_id=None)

    state = load_window_states(db, endpoint="e")[date(2026, 8, 20)]

    assert isinstance(state, WindowState)
