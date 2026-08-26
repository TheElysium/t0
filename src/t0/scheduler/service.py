"""Idempotent and resumable periodic polling loop (batch L6).

This module orchestrates :mod:`t0.scheduler.planning` (which windows to poll) and
:mod:`t0.scheduler.store` (persisted progress state) around an **injected**
downloader (:class:`WindowFetcher`): ``run_scheduled_poll`` knows neither
``garminconnect`` nor the network, and does not import
:class:`t0.garmin.client.GarminClient` -- this keeps this module testable
without network AND independent of batch L2's actual wiring (out of scope; the service
entry point is responsible for providing a ``WindowFetcher`` that calls ``GarminClient``
then returns the SHA256 hashes of the obtained payloads).

Central guarantee: **a failure mid-run never loses a window**. Each window is marked
``in_progress`` before the network call, ``done`` only after complete success; an
exception during fetch marks the window ``failed`` and the run continues with the next
windows -- neither interrupted nor restarted from the beginning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from t0.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    due_windows,
)
from t0.scheduler.store import (
    DBConnection,
    begin_window,
    complete_window,
    fail_window,
    has_payload_been_seen,
    load_window_states,
    record_payload_seen,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

logger = logging.getLogger(__name__)

#: Max windows processed PER ENDPOINT in a single run.
#:
#: Without this bound, the first run after install would start from ``history_start``
#: (Apr 2025) and chain over five hundred network calls in a row, per endpoint. The
#: rate limiter (:mod:`t0.garmin.rate_limit`) would space them without bounding
#: them: the run would last hours, and an unpublic API hammered this way is the fastest
#: way to get banned.
#:
#: The bound is safe because ``due_windows`` returns windows in strictly ASCENDING
#: chronological order: each run fills the oldest gaps, so catch-up progresses monotonically
#: and no window is ever abandoned -- only deferred. Applied per endpoint, not globally,
#: else a lagging endpoint would starve all following ones in the list.
DEFAULT_MAX_WINDOWS_PER_ENDPOINT = 60

#: One progress line every N windows within an endpoint.
#:
#: Without it a catch-up run is SILENT for minutes at a time: the loop below logs only
#: failures and truncations, so an operator watching a 300-window run cannot tell a slow
#: run from a hung one -- the only sign of life is the occasional archived activity file,
#: and the wellness endpoints produce none at all. Ten windows is roughly 15 s at the rate
#: limiter's 1.5 s floor: often enough to prove liveness, rare enough not to bury the
#: warnings that actually matter.
PROGRESS_LOG_EVERY_N_WINDOWS = 10


@dataclass(frozen=True, slots=True)
class FetchedWindow:
    """A payload obtained for a window, as reported by the injected :class:`WindowFetcher`.
    A window can yield multiple payloads (e.g. multiple activities on the same day):
    ``scope_key`` distinguishes them, ``None`` for day-aggregated endpoints (same
    convention as ``raw.api_response.scope_key``)."""

    scope_key: str | None
    payload_sha256: bytes


class WindowFetcher(Protocol):
    """Injected callable that performs the actual polling of a window.

    May raise any exception (network, authentication, parsing): :func:`run_scheduled_poll`
    catches it, marks the window ``failed`` and continues with the next windows -- a
    failure on one day must never block catch-up of other days or endpoints.
    """

    def __call__(self, endpoint: str, window_date: date) -> Sequence[FetchedWindow]: ...


@dataclass(slots=True)
class WindowOutcome:
    """Result of processing one window within a :class:`RunSummary`."""

    endpoint: str
    window_date: date
    error: str | None = None
    already_seen_count: int = 0
    new_payload_count: int = 0
    #: Non-null ``scope_key`` values reported by the fetcher for this window, in order
    #: received. For ``get_activities_by_date`` (only endpoint that produces them, cf.
    #: ``CallShape.ACTIVITIES_BY_DATE``) these are the day's ``activityId`` values: by
    #: these, ``python -m t0.scheduler`` knows which original files to archive,
    #: without re-querying the API for a list it just obtained. Populated for ALL payloads,
    #: already-seen or new: file presence on PVC is separate from JSON payload republication,
    #: and it's ``archive_activity_file`` that decides, from ``raw.fit_file``.
    scope_keys: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class RunSummary:
    """Summary of a call to :func:`run_scheduled_poll`, one entry per window processed."""

    outcomes: list[WindowOutcome] = field(default_factory=list)
    #: Endpoints whose due windows exceeded this run's bound: catch-up resumes at the
    #: next run. Set so truncation is never silent -- slowly progressing catch-up must
    #: be visible.
    truncated_endpoints: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> list[WindowOutcome]:
        return [outcome for outcome in self.outcomes if outcome.succeeded]

    @property
    def failed(self) -> list[WindowOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.succeeded]


def run_scheduled_poll(
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
    max_windows_per_endpoint: int | None = DEFAULT_MAX_WINDOWS_PER_ENDPOINT,
) -> RunSummary:
    """Execute one polling cycle on ``endpoints``, only on windows that are due.

    For each endpoint: load persisted state (:func:`~t0.scheduler.store.
    load_window_states`), compute due windows (:func:`~t0.scheduler.
    planning.due_windows`), then for each, in strictly ascending chronological order:

    1. mark it ``in_progress`` (persisted, before any network call);
    2. call ``fetch_window``;
    3. on exception: mark it ``failed`` with the error message, continue with the next
       window -- nothing is lost, nothing is reapplied twice;
    4. on success: for each obtained payload, query then update ``raw.payload_seen``
       (republish counter, never information loss), then mark the window ``done``.

    At most ``max_windows_per_endpoint`` windows are processed per endpoint per run
    (``None`` to unbind); remaining windows resume at the next run and the endpoint is
    flagged in ``RunSummary.truncated_endpoints``. See
    :data:`DEFAULT_MAX_WINDOWS_PER_ENDPOINT` for why.

    A window already ``done`` outside the refresh sliding window is simply never proposed
    by ``due_windows``: ``fetch_window`` is not called for it, avoiding unnecessary
    redownload after normal recovery.
    """
    active_now = now if now is not None else datetime.now(UTC)
    summary = RunSummary()
    for endpoint in endpoints:
        states = load_window_states(conn, endpoint=endpoint)
        pending_dates = due_windows(
            history_start=history_start,
            today=today,
            states=states,
            now=active_now,
            refresh_lookback_days=refresh_lookback_days,
            stale_after=stale_after,
        )
        if max_windows_per_endpoint is not None and len(pending_dates) > max_windows_per_endpoint:
            logger.info(
                "Endpoint %s: %d due windows, %d processed this run (bound); remaining "
                "windows resume at next run, oldest to newest.",
                endpoint,
                len(pending_dates),
                max_windows_per_endpoint,
            )
            summary.truncated_endpoints.append(endpoint)
            pending_dates = pending_dates[:max_windows_per_endpoint]

        if not pending_dates:
            logger.info("Endpoint %s: nothing due, skipped.", endpoint)
            continue
        logger.info(
            "Endpoint %s: %d window(s) due, %s to %s.",
            endpoint,
            len(pending_dates),
            pending_dates[0],
            pending_dates[-1],
        )
        succeeded = failed = 0

        for window_date in pending_dates:
            outcome = WindowOutcome(endpoint=endpoint, window_date=window_date)
            begin_window(
                conn, endpoint=endpoint, window_date=window_date, ingest_run_id=ingest_run_id
            )
            try:
                fetched = fetch_window(endpoint, window_date)
            except Exception as exc:
                logger.warning(
                    "Window %s/%s failed (%s): marked 'failed', replayable at next run.",
                    endpoint,
                    window_date,
                    exc,
                )
                fail_window(conn, endpoint=endpoint, window_date=window_date, error=str(exc))
                outcome.error = str(exc)
                summary.outcomes.append(outcome)
                failed += 1
                continue

            for item in fetched:
                if item.scope_key is not None:
                    outcome.scope_keys.append(item.scope_key)
                already_seen = has_payload_been_seen(
                    conn,
                    endpoint=endpoint,
                    scope_date=window_date,
                    scope_key=item.scope_key,
                    payload_sha256=item.payload_sha256,
                )
                if already_seen:
                    outcome.already_seen_count += 1
                else:
                    outcome.new_payload_count += 1
                record_payload_seen(
                    conn,
                    endpoint=endpoint,
                    scope_date=window_date,
                    scope_key=item.scope_key,
                    payload_sha256=item.payload_sha256,
                )
            complete_window(conn, endpoint=endpoint, window_date=window_date)
            summary.outcomes.append(outcome)
            succeeded += 1
            if (succeeded + failed) % PROGRESS_LOG_EVERY_N_WINDOWS == 0:
                logger.info(
                    "Endpoint %s: %d/%d window(s) processed (last: %s).",
                    endpoint,
                    succeeded + failed,
                    len(pending_dates),
                    window_date,
                )
        logger.info(
            "Endpoint %s: done, %d succeeded, %d failed out of %d window(s).",
            endpoint,
            succeeded,
            failed,
            len(pending_dates),
        )
    return summary
