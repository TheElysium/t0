"""Tests of :mod:`t0.ops.backfill` against
:class:`tests.scheduler.fakes.FakePollDB`, no PostgreSQL or network -- same test double
as ``tests/scheduler/test_service.py``, reused as-is (repo convention: see its import
in ``tests/scheduler/test_store.py``)."""

from __future__ import annotations

from datetime import date, datetime

from t0.ops.backfill import backfill_progress
from t0.scheduler.store import begin_window, complete_window, fail_window
from tests.scheduler.fakes import FakePollDB


def test_empty_db_has_all_windows_due_and_never_attempted() -> None:
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


def test_complete_backfill_leaves_no_windows_due() -> None:
    """``due_windows`` always resounds the current day window (day >= refresh_floor is
    true for ``day == today`` regardless of ``refresh_lookback_days`` >= 0): a 'done'
    window for today counts as not due only if it is recent ``in_progress`` (polling
    in progress), not ``done`` -- this is the only state representing "complete backfill,
    nothing more to do NOW"."""
    db = FakePollDB()
    db.clock = datetime(2026, 8, 21, 6, 0)
    for day in (18, 19, 20):
        window_date = date(2026, 8, day)
        begin_window(db, endpoint="get_sleep_data", window_date=window_date, ingest_run_id=None)
        complete_window(db, endpoint="get_sleep_data", window_date=window_date)
    begin_window(db, endpoint="get_sleep_data", window_date=date(2026, 8, 21), ingest_run_id=None)

    progress = backfill_progress(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 18),
        today=date(2026, 8, 21),
        now=db.clock,
        refresh_lookback_days=0,
    )

    assert progress.total_due == 0
    assert progress.fully_caught_up is True
    assert progress.by_endpoint[0].done == 3
    assert progress.by_endpoint[0].oldest_due is None


def test_failed_window_counted_in_both_due_and_failed() -> None:
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


def test_multiple_endpoints_reported_independently() -> None:
    """Progress is calculated per endpoint: backfill of one does not hide lag of another.
    Polling of ``get_sleep_data`` is fresh ``in_progress`` not ``done``, for the reason
    documented in :func:`test_complete_backfill_leaves_no_windows_due` -- a 'done'
    window for current day stays due regardless of ``refresh_lookback_days``."""
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
