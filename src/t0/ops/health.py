"""Operational health status of the pipeline (batch L10).

Each function in this module answers a question posed in real operations, and
ONLY from what actually exists in the database:

- "When did we last ingest per endpoint?" -- read from ``raw.poll_window.completed_at``
  (migration 009), which is only set on complete success (see
  ``t0.scheduler.store.complete_window``).
- "Which windows are failing?" -- ``raw.poll_window`` where ``status = 'failed'``.
- "Which windows are stuck ``in_progress``?" -- same table, ``status = 'in_progress'``
  and ``last_attempted_at`` older than the expiry threshold already defined by L6
  (:data:`t0.scheduler.planning.DEFAULT_STALE_IN_PROGRESS_AFTER`), reused here
  rather than redefined.
- "Are the ``mart`` views up to date?" -- comparison between ``MAX(local_date)`` of each
  view and ``MAX(local_date)`` of ``core.activity`` (most recent source data already
  transformed), plus ``pg_matviews.ispopulated``.
- "Where are the calendar gaps?" -- delegated to
  :func:`t0.ops.backfill.backfill_progress`, which already IS the answer to that
  question (a "due" window is a gap: never attempted, failed, or stuck).

No tables are invented: everything is read from ``raw.poll_window``, ``core.activity``,
and the system catalog ``pg_matviews``, all of which exist before this batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from t0.ops.refresh import MART_VIEWS
from t0.scheduler.planning import DEFAULT_STALE_IN_PROGRESS_AFTER

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import timedelta

    import psycopg


@dataclass(frozen=True, slots=True)
class LastSuccess:
    """Last successful window for an endpoint (``None`` if never)."""

    endpoint: str
    last_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class FailedWindow:
    """A ``failed`` window as persisted in ``raw.poll_window``."""

    endpoint: str
    window_date: date
    attempts: int
    last_attempted_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StuckWindow:
    """An ``in_progress`` window older than the expiry threshold."""

    endpoint: str
    window_date: date
    attempts: int
    last_attempted_at: datetime | None


@dataclass(frozen=True, slots=True)
class MartViewFreshness:
    """Freshness of a ``mart`` view relative to ``core.activity``."""

    view: str
    populated: bool
    #: ``None`` if view is empty (populated but no rows, or not populated).
    latest_local_date: date | None
    #: ``None`` if ``core.activity`` is empty.
    latest_core_local_date: date | None

    @property
    def lag_days(self) -> int | None:
        """Lag in days between most recent source data and view. ``None`` if either
        date is missing (nothing to compare)."""
        if self.latest_local_date is None or self.latest_core_local_date is None:
            return None
        return (self.latest_core_local_date - self.latest_local_date).days


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregated health report produced by :func:`build_health_report`."""

    generated_at: datetime
    last_success: list[LastSuccess]
    failed_windows: list[FailedWindow]
    stuck_windows: list[StuckWindow]
    mart_freshness: list[MartViewFreshness]


def last_success_by_endpoint(conn: psycopg.Connection[tuple[Any, ...]]) -> list[LastSuccess]:
    """``MAX(completed_at)`` per endpoint among ``done`` windows.

    An endpoint appearing in ``raw.poll_window`` only with ``pending``/``failed``/
    ``in_progress`` windows (never ``done``) still appears in the result with
    ``last_completed_at = None`` -- this is precisely what an operator needs to see
    (endpoint that never succeeded once), not a case to hide with ``WHERE
    completed_at IS NOT NULL``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT endpoint, MAX(completed_at) FROM raw.poll_window "
            "GROUP BY endpoint ORDER BY endpoint;"
        )
        rows = cur.fetchall()
    return [LastSuccess(endpoint=row[0], last_completed_at=row[1]) for row in rows]


def failed_windows(conn: psycopg.Connection[tuple[Any, ...]]) -> list[FailedWindow]:
    """All currently ``failed`` windows, oldest first.

    Ops note: a ``failed`` window is auto-retried on next poll
    (:func:`t0.scheduler.planning.due_windows` considers it due) -- this list
    is a monitoring signal, not a queue to handle manually.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT endpoint, window_date, attempts, last_attempted_at, last_error "
            "FROM raw.poll_window WHERE status = 'failed' ORDER BY window_date;"
        )
        rows = cur.fetchall()
    return [
        FailedWindow(
            endpoint=row[0],
            window_date=row[1],
            attempts=row[2],
            last_attempted_at=row[3],
            last_error=row[4],
        )
        for row in rows
    ]


def stuck_in_progress_windows(
    conn: psycopg.Connection[tuple[Any, ...]],
    *,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
    now: datetime | None = None,
) -> list[StuckWindow]:
    """``in_progress`` windows older than ``stale_after``.

    Same threshold as :func:`t0.scheduler.planning.due_windows` by default: a
    window listed here will be retried on next poll anyway. This function serves to
    MONITOR it (e.g., alert if count stays high run-to-run, sign polling stopped),
    not to act on it -- no manual action needed: see ``docs/RUNBOOK.md``.
    """
    active_now = now if now is not None else datetime.now(UTC)
    threshold = active_now - stale_after
    with conn.cursor() as cur:
        cur.execute(
            "SELECT endpoint, window_date, attempts, last_attempted_at "
            "FROM raw.poll_window WHERE status = 'in_progress' "
            "AND (last_attempted_at IS NULL OR last_attempted_at < %s) "
            "ORDER BY last_attempted_at NULLS FIRST;",
            (threshold,),
        )
        rows = cur.fetchall()
    return [
        StuckWindow(endpoint=row[0], window_date=row[1], attempts=row[2], last_attempted_at=row[3])
        for row in rows
    ]


def mart_views_freshness(
    conn: psycopg.Connection[tuple[Any, ...]], *, views: Iterable[str] = MART_VIEWS
) -> list[MartViewFreshness]:
    """Freshness of each view in ``views`` relative to ``core.activity``.

    ``latest_core_local_date`` is calculated ONCE (same value for all views): this is
    the most recent source data already transformed by batches L1-L5, hence the freshness
    ceiling a ``REFRESH`` can achieve.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(local_date) FROM core.activity;")
        row = cur.fetchone()
    latest_core = row[0] if row is not None else None

    results: list[MartViewFreshness] = []
    for view in views:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ispopulated FROM pg_matviews "
                "WHERE schemaname = 'mart' AND matviewname = %s;",
                (view.split(".", 1)[1],),
            )
            pop_row = cur.fetchone()
        populated = bool(pop_row[0]) if pop_row is not None else False
        latest_view_date = None
        if populated:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(local_date) FROM {view};")  # noqa: S608 -- `view` from MART_VIEWS, never user input
                view_row = cur.fetchone()
            latest_view_date = view_row[0] if view_row is not None else None
        results.append(
            MartViewFreshness(
                view=view,
                populated=populated,
                latest_local_date=latest_view_date,
                latest_core_local_date=latest_core,
            )
        )
    return results


def build_health_report(
    conn: psycopg.Connection[tuple[Any, ...]],
    *,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
    now: datetime | None = None,
) -> HealthReport:
    """Assemble full report in one call -- convenient for CLI and for possible
    export to metrics exporter later (out of scope here)."""
    active_now = now if now is not None else datetime.now(UTC)
    return HealthReport(
        generated_at=active_now,
        last_success=last_success_by_endpoint(conn),
        failed_windows=failed_windows(conn),
        stuck_windows=stuck_in_progress_windows(conn, stale_after=stale_after, now=active_now),
        mart_freshness=mart_views_freshness(conn),
    )


#: Maximum windows listed by name in text report. Beyond this, only total is shown: a
#: health report that unrolls hundreds of lines in console (or CronJob logs) stops
#: being read. Full detail still queryable in SQL from ``raw.poll_window``.
_MAX_LISTED_LINES = 20


def format_health_report(report: HealthReport) -> str:
    """Simple text rendering, similar spirit to
    :func:`t0.db.runner.format_status`."""
    lines = [
        f"Health check at {report.generated_at.isoformat()}",
        "",
        "Last success per endpoint:",
    ]
    if report.last_success:
        for entry in report.last_success:
            completed = entry.last_completed_at.isoformat() if entry.last_completed_at else "NEVER"
            lines.append(f"  {entry.endpoint}: {completed}")
    else:
        lines.append("  (no endpoints in raw.poll_window)")

    lines.append("")
    lines.append(f"Failed windows ({len(report.failed_windows)}):")
    lines.extend(
        f"  {fw.endpoint} / {fw.window_date} " f"(attempts={fw.attempts}): {fw.last_error}"
        for fw in report.failed_windows[:_MAX_LISTED_LINES]
    )
    if len(report.failed_windows) > _MAX_LISTED_LINES:
        lines.append(f"  ... and {len(report.failed_windows) - _MAX_LISTED_LINES} more.")

    lines.append("")
    lines.append(f"Stuck in_progress windows ({len(report.stuck_windows)}):")
    lines.extend(
        f"  {sw.endpoint} / {sw.window_date} " f"(last attempt {sw.last_attempted_at})"
        for sw in report.stuck_windows[:_MAX_LISTED_LINES]
    )
    if len(report.stuck_windows) > _MAX_LISTED_LINES:
        lines.append(f"  ... and {len(report.stuck_windows) - _MAX_LISTED_LINES} more.")

    lines.append("")
    lines.append("Mart view freshness:")
    for mv in report.mart_freshness:
        if not mv.populated:
            lines.append(f"  {mv.view}: NEVER POPULATED")
        else:
            lag = mv.lag_days
            lag_txt = f"{lag}d behind" if lag else "current"
            lines.append(f"  {mv.view}: {mv.latest_local_date} ({lag_txt})")

    return "\n".join(lines)
