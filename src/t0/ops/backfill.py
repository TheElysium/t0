"""Tracking and steering of historical backfill from ``history_start`` (batch L10).

This module reimplements NOTHING from :mod:`t0.scheduler`: it composes
``t0.scheduler.store.load_window_states`` (reading persisted state) and
``t0.scheduler.planning.due_windows`` (pure calculation of due windows) --
exactly the two functions that :func:`t0.scheduler.service.run_scheduled_poll`
uses internally -- to produce a progress report. It invents no notion of
"remaining window" different from the one already defined by L6.

This module does NOT know how to query Garmin. Actually bootstrapping backfill (calling
``fetch_window`` for real) requires an adapter that translates one of the ~130
``garminconnect`` endpoint names to the real call of :class:`t0.garmin.client.
GarminClient` -- this adapter and the list of endpoints to poll do not exist anywhere
else in the repo to date (no service entry point wires :mod:`t0.scheduler` to
:mod:`t0.garmin` -- see batch end report). Guessing this list or wiring here would
invent a contract that this batch cannot verify. :func:`run_backfill_cycle` below thus
remains a simple typed relay to :func:`t0.scheduler.service.run_scheduled_poll`,
ready to be called by that future entry point with a real ``fetch_window`` --
WITHOUT any logic of its own to duplicate later. Until that wiring, operations has
:func:`backfill_progress`, which alone answers the actual operational question:
"how many windows remain, and since when?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from t0.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    daily_range,
    due_windows,
)
from t0.scheduler.service import (
    RunSummary,
    WindowFetcher,
    run_scheduled_poll,
)
from t0.scheduler.store import DBConnection, load_window_states

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, timedelta

#: Actual start of available history for the athlete (cf. brief L10). Distinct from
#: ``t0.scheduler.planning`` (which sets no default date: history start is a
#: call parameter, not a planning constant) -- this is a PRODUCT fact (athlete's Garmin
#: account has no usable history before this date), not an algorithm choice.
DEFAULT_HISTORY_START_ISO = "2025-04-03"


@dataclass(frozen=True, slots=True)
class EndpointBackfillProgress:
    """Backfill progress for an endpoint over ``[history_start, today]``."""

    endpoint: str
    total_days: int
    done: int
    due: int
    #: Subset of ``due``: windows never attempted.
    never_attempted: int
    #: Subset of ``due``: failed windows (auto-retried).
    failed: int
    #: Subset of ``due``: stale ``in_progress`` windows (run interrupted).
    stale_in_progress: int
    #: Oldest due window, ``None`` if none (backfill complete for this endpoint,
    #: modulo sliding window freshness).
    oldest_due: date | None

    @property
    def is_caught_up(self) -> bool:
        """True if no windows are due -- historical backfill for this endpoint is
        complete (will only resound the recent sliding window)."""
        return self.due == 0


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    """Progress report over multiple endpoints, produced by :func:`backfill_progress`."""

    history_start: date
    today: date
    by_endpoint: list[EndpointBackfillProgress] = field(default_factory=list)

    @property
    def total_due(self) -> int:
        return sum(p.due for p in self.by_endpoint)

    @property
    def fully_caught_up(self) -> bool:
        return all(p.is_caught_up for p in self.by_endpoint)


def backfill_progress(
    conn: DBConnection,
    *,
    endpoints: Sequence[str],
    history_start: date,
    today: date,
    now: datetime | None = None,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
) -> BackfillProgress:
    """Compute backfill progress for each endpoint.

    Idempotent and side-effect free: reads ``raw.poll_window`` (via
    :func:`~t0.scheduler.store.load_window_states`), never modifies anything.
    Re-executable at will -- exactly what an ops checkpoint requires.

    ``due`` (and its subcategories) reuse :func:`~t0.scheduler.planning.
    due_windows` as-is: these are EXACTLY the windows that a
    :func:`~t0.scheduler.service.run_scheduled_poll` would handle on next run.
    """
    active_now = now if now is not None else datetime.now(UTC)
    total_days = len(daily_range(history_start, today))
    by_endpoint: list[EndpointBackfillProgress] = []
    for endpoint in endpoints:
        states = load_window_states(conn, endpoint=endpoint)
        due = due_windows(
            history_start=history_start,
            today=today,
            states=states,
            now=active_now,
            refresh_lookback_days=refresh_lookback_days,
            stale_after=stale_after,
        )
        never_attempted = sum(1 for d in due if d not in states)
        failed = sum(1 for d in due if d in states and states[d].status == "failed")
        stale_in_progress = sum(1 for d in due if d in states and states[d].status == "in_progress")
        done = sum(1 for state in states.values() if state.status == "done")
        by_endpoint.append(
            EndpointBackfillProgress(
                endpoint=endpoint,
                total_days=total_days,
                done=done,
                due=len(due),
                never_attempted=never_attempted,
                failed=failed,
                stale_in_progress=stale_in_progress,
                oldest_due=due[0] if due else None,
            )
        )
    return BackfillProgress(history_start=history_start, today=today, by_endpoint=by_endpoint)


def run_backfill_cycle(
    conn: DBConnection,
    *,
    endpoints: Sequence[str],
    history_start: date,
    today: date,
    fetch_window: WindowFetcher,
    ingest_run_id: int | None = None,
    now: datetime | None = None,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
    max_windows_per_endpoint: int | None = None,
) -> RunSummary:
    """Typed relay to :func:`t0.scheduler.service.run_scheduled_poll`.

    Exists to give backfill a named, documented entry point in :mod:`t0.ops`,
    without duplicating any planning or recovery logic -- all delegated. The only
    difference from a direct call to ``run_scheduled_poll`` is the default for
    ``max_windows_per_endpoint``: ``None`` here (unbounded) rather than
    :data:`t0.scheduler.service.DEFAULT_MAX_WINDOWS_PER_ENDPOINT` (60),
    because a backfill explicitly triggered by an operator (this Job, not periodic
    polling) should process the entire remainder in one pass rather than spread over
    multiple cycles of 60 windows. A caller preferring the usual bound can pass it
    explicitly.
    """
    return run_scheduled_poll(
        conn,
        endpoints=endpoints,
        history_start=history_start,
        today=today,
        fetch_window=fetch_window,
        ingest_run_id=ingest_run_id,
        now=now,
        refresh_lookback_days=refresh_lookback_days,
        stale_after=stale_after,
        max_windows_per_endpoint=max_windows_per_endpoint,
    )
