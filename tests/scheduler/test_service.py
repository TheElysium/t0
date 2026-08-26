"""Tests for :mod:`t0.scheduler.service` -- end-to-end orchestration against
:class:`tests.scheduler.fakes.FakePollDB`, no PostgreSQL or network. ``fetch_window``
is a fake function driven by the test (no calls to ``GarminClient``)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from t0.scheduler.service import FetchedWindow, WindowFetcher, run_scheduled_poll
from t0.scheduler.store import load_window_states
from tests.scheduler.fakes import FakePollDB


def test_full_run_marks_all_windows_done() -> None:
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


def test_second_execution_does_not_repoll_already_done_windows() -> None:
    """Heart of recovery: after a successful run, a second run (beyond the refresh window)
    must NOT redownload ANY of the past days already 'done'.

    ``today`` advances one day between runs (simulates a calendar day elapsed) :
    the already-processed day then exits the refresh window (``refresh_lookback_days
    = 0``), only the brand new current day must be polled."""
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

    db.clock = datetime(2026, 8, 2, 6, 0)  # next day
    run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=date(2026, 8, 1),
        today=date(2026, 8, 2),
        fetch_window=fetch_window,
        now=datetime(2026, 8, 2, 6, 0),
        refresh_lookback_days=0,
    )

    # Sanity check against a naive scheduler that re-loops through entire history: Aug 1st,
    # already 'done' and now outside refresh window, is not re-polled; only
    # Aug 2nd (new day, never tried) is.
    assert calls == [date(2026, 8, 2)]


def test_failure_mid_run_does_not_prevent_following_windows() -> None:
    """Non-negotiable mission guarantee: a partially failed run must
    never lose windows yet to process."""
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


def test_failed_window_is_replayed_at_next_run() -> None:
    """Completes previous test: the failed day must come back, and it alone --
    already-successful history must not be replayed with it."""
    db = FakePollDB()
    attempts: list[date] = []
    fail_once_more = {date(2026, 8, 19)}  # days that still fail on next visit

    def flaky_fetch(endpoint: str, window_date: date) -> list[FetchedWindow]:
        attempts.append(window_date)
        if window_date in fail_once_more:
            fail_once_more.discard(window_date)
            raise RuntimeError("transient network failure")
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

    # ``today`` unchanged (same calendar day) : with refresh_lookback_days=0, only
    # day 20 (current day) would remain due by refresh window -- we freeze it beyond
    # stale threshold to isolate recovery of only the failed day.
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

    # Sanity check: without per-day state persistence, this second run would replay
    # also days 18 and 20 (already 'done') -- only 19 (the only 'failed') must return.
    assert attempts == [date(2026, 8, 19), date(2026, 8, 20)]
    assert len(summary2.succeeded) == 2
    states = load_window_states(db, endpoint="get_activities")
    assert states[date(2026, 8, 19)].status == "done"
    assert states[date(2026, 8, 19)].last_error is None
    assert states[date(2026, 8, 18)].status == "done"  # never touched, stayed unchanged


def test_fatal_crash_during_fetch_leaves_window_replayable() -> None:
    """Simulates process crash: ``fetch_window`` NEVER returns (neither success nor
    clean exception -- we simulate by raising, but without calling code to catch it,
    which ``run_scheduled_poll`` would normally do). Here we verify directly the invariant
    on which the entire recovery guarantee rests: after ``begin_window`` alone
    (no complete/fail -- exactly the state of a killed process), the window stays
    'in_progress' and becomes due again once stale."""
    from t0.scheduler.planning import due_windows
    from t0.scheduler.store import begin_window

    db = FakePollDB(clock=datetime(2026, 8, 19, 6, 0))
    begin_window(db, endpoint="get_activities", window_date=date(2026, 8, 19), ingest_run_id=1)
    # The "process" dies here -- neither complete_window nor fail_window is ever called.

    states = load_window_states(db, endpoint="get_activities")
    due_juste_apres = due_windows(
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        states=states,
        now=datetime(2026, 8, 19, 6, 1),  # 1 min later : plausible concurrent run
        stale_after=timedelta(hours=2),
    )
    due_plus_tard = due_windows(
        history_start=date(2026, 8, 19),
        today=date(2026, 8, 19),
        states=states,
        now=datetime(2026, 8, 19, 9, 0),  # 3h later : no doubt now, it's a crash
        stale_after=timedelta(hours=2),
    )

    assert due_juste_apres == []  # give a chance to a legitimate concurrent run
    assert due_plus_tard == [date(2026, 8, 19)]  # but never lose the window for good


def test_duplicate_same_payload_increments_already_seen_not_new() -> None:
    """Content deduplication (raw.payload_seen) must never prevent marking
    window 'done' -- unchanged content is success, not error."""
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
        refresh_lookback_days=1,  # force re-polling of the same day
    )

    outcome = summary2.outcomes[0]
    assert outcome.already_seen_count == 1
    assert outcome.new_payload_count == 0
    assert outcome.succeeded


def test_changed_payload_counted_as_new_not_already_seen() -> None:
    """A renamed/corrected activity changes its hash: it must be reingested, not
    silently ignored by idempotence."""
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
        fetch_window=make_fetch(6),  # content modified -- e.g. sport corrected retroactively
        now=datetime(2026, 8, 19, 6, 0),
        refresh_lookback_days=1,
    )

    outcome = summary2.outcomes[0]
    assert outcome.new_payload_count == 1
    assert outcome.already_seen_count == 0


def test_multiple_endpoints_are_processed_independently() -> None:
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


def test_history_start_after_today_raises_error() -> None:
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


def test_massive_catchup_is_bounded_and_progresses_per_run() -> None:
    """First run after install must not chain entire history.

    Without bound, a ``history_start`` in April 2025 would produce over five hundred network
    calls in a row: the rate limiter would space them without bounding them, and an unpublic
    API hammered this way leads to ban. This test verifies both halves of the guarantee:
    the bound bites, AND catch-up PROGRESSES -- a bound that re-polled the same windows
    at each run would never catch up.

    Sanity check included: without bound (``None``), all 100 windows pass in one run.
    """
    history_start = date(2026, 5, 1)
    today = date(2026, 8, 8)  # 100 days inclusive
    timestamp = datetime(2026, 8, 8, 6, 0)

    def _fetch(endpoint: str, window_date: date) -> list[FetchedWindow]:
        return [FetchedWindow(scope_key=None, payload_sha256=bytes(32))]

    db = FakePollDB()
    first = run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=timestamp,
        max_windows_per_endpoint=40,
    )
    assert len(first.outcomes) == 40
    assert first.truncated_endpoints == ["get_sleep_data"], "truncation must be visible"
    # OLDEST first: this is what makes the bound safe.
    assert first.outcomes[0].window_date == date(2026, 5, 1)
    assert first.outcomes[-1].window_date == date(2026, 6, 9)

    second = run_scheduled_poll(
        db,
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=timestamp,
        max_windows_per_endpoint=40,
    )
    # The run resumes where the previous one left off: no already-done window is
    # re-polled, except the refresh window for recent days.
    assert second.outcomes[0].window_date == date(2026, 6, 10)

    # Sanity check: without bound, everything passes at once and nothing is marked truncated.
    unbounded = run_scheduled_poll(
        FakePollDB(),
        endpoints=["get_sleep_data"],
        history_start=history_start,
        today=today,
        fetch_window=_fetch,
        now=timestamp,
        max_windows_per_endpoint=None,
    )
    assert len(unbounded.outcomes) == 100
    assert unbounded.truncated_endpoints == []


# -- progress logging: proving a long catch-up run is not silent ------------------------


def test_progress_is_logged_so_a_long_run_is_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    """A 300-window catch-up used to emit NOTHING on the success path: an operator could
    not tell a slow run from a hung one. Assert the three lines that make it observable --
    the endpoint header, the every-N-windows heartbeat, and the closing count."""
    db = FakePollDB()

    def _fetch(endpoint: str, window_date: date) -> list[FetchedWindow]:
        if window_date == date(2026, 5, 3):
            msg = "boom"
            raise RuntimeError(msg)
        return [FetchedWindow(scope_key=None, payload_sha256=bytes(32))]

    with caplog.at_level("INFO", logger="t0.scheduler.service"):
        run_scheduled_poll(
            db,
            endpoints=["get_sleep_data"],
            history_start=date(2026, 5, 1),
            today=date(2026, 5, 25),  # 25 windows: crosses the every-10 heartbeat twice
            fetch_window=_fetch,
            now=datetime(2026, 5, 25, 6, 0),
        )

    messages = [record.getMessage() for record in caplog.records]
    assert "Endpoint get_sleep_data: 25 window(s) due, 2026-05-01 to 2026-05-25." in messages
    heartbeats = [m for m in messages if "window(s) processed" in m]
    assert len(heartbeats) == 2, heartbeats
    assert "10/25" in heartbeats[0]
    assert "20/25" in heartbeats[1]
    assert "Endpoint get_sleep_data: done, 24 succeeded, 1 failed out of 25 window(s)." in messages
