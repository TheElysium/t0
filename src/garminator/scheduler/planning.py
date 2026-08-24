"""Pure planning of polling windows (batch L6).

No function in this module accesses the database or network: they receive the known state
of windows (as read from ``raw.poll_window`` by :mod:`garminator.scheduler.store`)
as argument and return dates to (re)poll. This is what makes them testable without PostgreSQL.

Scheduling principle (cf. ``COMMENT ON TABLE raw.poll_window``, migration 009):
a window is due if

1. it has never been tried (history catch-up / new install) ;
2. its last attempt failed (``failed``) -- it is never silently abandoned ;
3. it is stuck ``in_progress`` for longer than ``stale_after`` -- residue
   of an interrupted run (crash, ``OOMKilled``, Kubernetes ``SIGTERM``) : without this case,
   a crash mid-run would leave the window indefinitely stuck, never replayed,
   and the corresponding activity would be lost ;
4. it is ``done`` but falls in the recent sliding window (``refresh_lookback_days``)
   -- Garmin sometimes republishes a day later (activity renamed, sport corrected,
   late arrival), and idempotence must not prevent this legitimate reingestion.

A ``done`` window outside the sliding window is NEVER due: this is what
avoids redownloading the entire history after a simple restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Values allowed by ``poll_window_status_ck`` (migration 009).
WindowStatusLiteral = Literal["pending", "in_progress", "done", "failed"]

#: Default sliding window systematically re-polled even if already ``done`` :
#: catches late Garmin republications without repassing through entire history.
DEFAULT_REFRESH_LOOKBACK_DAYS = 3

#: Duration after which an ``in_progress`` window is considered
#: residue of an interrupted run rather than truly concurrent polling.
DEFAULT_STALE_IN_PROGRESS_AFTER = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class WindowState:
    """Known state of a window (endpoint, day), as read from ``raw.poll_window``."""

    status: WindowStatusLiteral
    attempts: int
    last_attempted_at: datetime | None
    last_error: str | None


def daily_range(start: date, end: date) -> list[date]:
    """All dates from ``start`` to ``end`` inclusive, ascending order.

    Raises:
        ValueError: if ``start`` is after ``end``.
    """
    if start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def due_windows(
    *,
    history_start: date,
    today: date,
    states: Mapping[date, WindowState],
    now: datetime,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
) -> list[date]:
    """Determines windows to (re)poll, in ascending chronological order.

    Ascending order is deliberate: history catch-up after a long crash
    fills oldest gaps first rather than polling current day in a loop
    while the past is not caught up.

    Raises:
        ValueError: if ``refresh_lookback_days`` is negative, or if ``history_start`` is
            after ``today``.
    """
    if refresh_lookback_days < 0:
        raise ValueError("refresh_lookback_days must be >= 0")
    refresh_floor = today - timedelta(days=refresh_lookback_days)

    due: list[date] = []
    for day in daily_range(history_start, today):
        state = states.get(day)
        if state is None or state.status in ("pending", "failed"):
            due.append(day)
        elif state.status == "in_progress":
            attempted_at = state.last_attempted_at
            if attempted_at is None or (now - attempted_at) > stale_after:
                due.append(day)
        elif state.status == "done" and day >= refresh_floor:
            due.append(day)
    return due
