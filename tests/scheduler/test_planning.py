"""Tests for :mod:`garminator.scheduler.planning` -- pure, no DB or network."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from garminator.scheduler.planning import WindowState, daily_range, due_windows


def test_daily_range_is_inclusive_on_both_bounds() -> None:
    days = daily_range(date(2026, 8, 1), date(2026, 8, 3))

    assert days == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


def test_daily_range_single_day() -> None:
    assert daily_range(date(2026, 8, 1), date(2026, 8, 1)) == [date(2026, 8, 1)]


def test_daily_range_rejects_inverted_interval() -> None:
    # Sanity check: without this guard, a silent inversion would return []
    # instead of signaling a call error.
    with pytest.raises(ValueError, match="must be <="):
        daily_range(date(2026, 8, 5), date(2026, 8, 1))


class TestDueWindowsHistory:
    def test_untried_window_is_due(self) -> None:
        due = due_windows(
            history_start=date(2026, 8, 18),
            today=date(2026, 8, 18),
            states={},
            now=datetime(2026, 8, 18, 6, 0),
        )

        assert due == [date(2026, 8, 18)]

    def test_done_window_outside_refresh_window_is_not_due(self) -> None:
        """Falsifiability: without the freshness filter, this old 'done' window
        would be re-proposed at each run, redownloading entire history."""
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

    def test_done_window_in_refresh_window_remains_due(self) -> None:
        """Catches late republication by Garmin (activity renamed, corrected)."""
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
        assert date(2026, 8, 10) in due  # current day, never tried

    def test_failed_window_is_always_due(self) -> None:
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


class TestDueWindowsCrash:
    def test_recent_in_progress_is_not_reconsidered_due(self) -> None:
        """A window in progress (concurrent run or not yet expired) must not
        be re-proposed -- otherwise the same day would be polled twice in parallel."""
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

    def test_stale_in_progress_is_replayed_after_crash(self) -> None:
        """The core of recovery guarantee: a run that died mid-flight (frozen in_progress)
        must never lose the window. Sanity check: without this case, the window
        would stay in_progress forever, never replayed -- exactly the loss forbidden by spec."""
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

    def test_in_progress_without_timestamp_is_always_stale(self) -> None:
        """Defensive case: an in_progress row without last_attempted_at (should not
        arrive via begin_window, but must never silently block)."""
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


def test_due_windows_ascending_chronological_order() -> None:
    """History catch-up: oldest gaps first, never current day in a loop
    until the past is filled."""
    due = due_windows(
        history_start=date(2026, 8, 1),
        today=date(2026, 8, 3),
        states={},
        now=datetime(2026, 8, 3, 6, 0),
    )

    assert due == [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


def test_due_windows_rejects_negative_lookback() -> None:
    with pytest.raises(ValueError, match="refresh_lookback_days"):
        due_windows(
            history_start=date(2026, 8, 1),
            today=date(2026, 8, 1),
            states={},
            now=datetime(2026, 8, 1, 6, 0),
            refresh_lookback_days=-1,
        )
