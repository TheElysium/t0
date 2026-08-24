"""Batch L6: periodic poll scheduling and idempotence.

Three separate responsibilities:

- :mod:`garminator.scheduler.planning`: pure logic (no DB, no network) deciding which
  ``(endpoint, day)`` windows are due.
- :mod:`garminator.scheduler.store`: read/write ``raw.poll_window`` (migration 009,
  poll progress) and ``raw.payload_seen`` (migration 002, content deduplication),
  behind an injected connection.
- :mod:`garminator.scheduler.service`: orchestrates both around an injected download
  (:class:`~garminator.scheduler.service.WindowFetcher`), never building its own
  :class:`garminator.garmin.client.GarminClient`—that real wiring is the service
  entry point's job, out of this batch's scope.

Guarantee upheld by this package: a run interrupted mid-way (crash, ``SIGTERM``)
never loses a window—it is replayed next run, never silently marked complete.
"""

from __future__ import annotations

from garminator.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    WindowState,
    WindowStatusLiteral,
    daily_range,
    due_windows,
)
from garminator.scheduler.service import (
    FetchedWindow,
    RunSummary,
    WindowFetcher,
    WindowOutcome,
    run_scheduled_poll,
)
from garminator.scheduler.store import (
    DBConnection,
    begin_window,
    complete_window,
    fail_window,
    has_payload_been_seen,
    load_window_states,
    record_payload_seen,
)

__all__ = [
    "DEFAULT_REFRESH_LOOKBACK_DAYS",
    "DEFAULT_STALE_IN_PROGRESS_AFTER",
    "DBConnection",
    "FetchedWindow",
    "RunSummary",
    "WindowFetcher",
    "WindowOutcome",
    "WindowState",
    "WindowStatusLiteral",
    "begin_window",
    "complete_window",
    "daily_range",
    "due_windows",
    "fail_window",
    "has_payload_been_seen",
    "load_window_states",
    "record_payload_seen",
    "run_scheduled_poll",
]
