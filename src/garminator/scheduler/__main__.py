"""Entry point for the scheduled service: ``python -m garminator.scheduler [options]``.

This module—and it alone—may build a real ``garminconnect.Garmin`` transport: the
rest of the package (``planning``, ``store``, ``service``, ``catalog``, ``fetcher``)
stays testable without network because it only knows injected protocols
(:class:`~garminator.scheduler.service.WindowFetcher`,
:class:`~garminator.scheduler.store.DBConnection`). This is exactly the role
``client.py`` (batch L2) and ``service.py`` (batch L6) both documented as left to a
future entry point—this one.

Same style as :mod:`garminator.ops.__main__` and :mod:`garminator.db.__main__`: plain
``argparse``, optional ``--dsn`` (prefer env var in production; the argument exposes
the password in process list), non-zero exit whenever there's something to alert the
operator—here, whenever at least one window failed. A Kubernetes ``CronJob`` that
always exits ``0`` never surfaces a poll failure: that is exactly what this module
prevents.

``main`` accepts injection points (``settings``, ``connect_db``,
``build_window_fetcher``) solely for testing: each has real production behavior as its
default, but no test in this batch exercises the defaults—they systematically inject
fakes to never build real ``garminconnect`` transport or open real PostgreSQL.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

import garminconnect

from garminator import __version__
from garminator.db.runner import MigrationError, connect, resolve_dsn
from garminator.garmin.client import GarminClient, ingest_run
from garminator.logging_setup import configure_logging
from garminator.scheduler.activity_files import archive_activities_for_day
from garminator.scheduler.catalog import DEFAULT_ENDPOINTS, is_known_endpoint
from garminator.scheduler.fetcher import GarminWindowFetcher
from garminator.scheduler.planning import DEFAULT_REFRESH_LOOKBACK_DAYS, due_windows
from garminator.scheduler.service import DEFAULT_MAX_WINDOWS_PER_ENDPOINT, run_scheduled_poll
from garminator.scheduler.store import load_window_states
from garminator.settings import Settings, SettingsError, get_settings

logger = logging.getLogger(__name__)

#: Only catalog endpoint producing ``scope_key`` = ``activityId``
#: (``CallShape.ACTIVITIES_BY_DATE``), so the only one through which this module knows
#: which original files to archive.
_ACTIVITIES_ENDPOINT = "get_activities_by_date"

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from garminator.garmin.client import IngestRunTracker
    from garminator.scheduler.activity_files import OriginalFileDownloader
    from garminator.scheduler.service import RunSummary, WindowFetcher
    from garminator.scheduler.store import DBConnection

#: Fallback for ``history_start`` when neither ``--history-start`` nor loaded
#: ``Settings`` are available—notably in ``--dry-run`` with no ``GARMINATOR_*``
#: variable set (fix C2: dry-run must not depend on Garmin secrets). Value identical
#: to ``Settings.history_start`` (``garminator.settings``) and
#: ``garminator.ops.backfill.DEFAULT_HISTORY_START_ISO``—duplicated rather than
#: imported for the same reason those two already duplicate each other: this module
#: must work (in dry-run) without ever building ``Settings``, so without depending
#: on ``garminator.settings`` for this one constant; and it must also not depend on
#: ``garminator.ops`` for a module with no other reason to know that ops package.
#: ``tests/test_settings.py`` already locks equality between the first two; this
#: module cannot test itself against them without reintroducing the dependency it
#: avoids—any future drift must be caught by eye during review.
_FALLBACK_HISTORY_START = date(2025, 4, 3)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.scheduler",
        description=(
            "Scheduled periodic poll (batch L11): catches up history then keeps"
            "[endpoint, day] windows current via garminconnect, never losing a"
            "window on partial failure."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL DSN. If not set, read from GARMINATOR_POSTGRES_DSN (via "
            "app config). Prefer env var: a DSN passed here appears in plaintext "
            "in the process list."
        ),
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        default=None,
        help=(
            "Garminconnect endpoint to poll (repeatable: --endpoint get_sleep_data "
            "--endpoint get_hrv_data ...). Default: relevant subset for triathlete "
            "(garminator.scheduler.catalog.DEFAULT_ENDPOINTS)."
        ),
    )
    parser.add_argument(
        "--today",
        default=None,
        type=date.fromisoformat,
        help="Date to consider (default: today's actual date, UTC).",
    )
    parser.add_argument(
        "--history-start",
        default=None,
        type=date.fromisoformat,
        help=(
            "History catchup start date (default: GARMINATOR_HISTORY_START, or "
            "2025-04-03 if unconfigured)."
        ),
    )
    parser.add_argument(
        "--refresh-lookback-days",
        default=None,
        type=int,
        help=(
            "Refresh sliding window in days (default: "
            "GARMINATOR_REFRESH_LOOKBACK_DAYS, or 3 if unconfigured)."
        ),
    )
    parser.add_argument(
        "--max-windows-per-endpoint",
        default=DEFAULT_MAX_WINDOWS_PER_ENDPOINT,
        type=int,
        help=(
            "Limit of windows processed per endpoint per run (default: "
            f"{DEFAULT_MAX_WINDOWS_PER_ENDPOINT})."
        ),
    )
    parser.add_argument(
        "--no-activity-files",
        action="store_true",
        help=(
            "Do NOT download original (.FIT/.TCX) files of polled activities. By "
            "default they are archived on PVC right after poll: it is the only source "
            "of second-by-second data that JSON API does not provide. Use only for "
            "pure-JSON catchup run or to avoid the download surge of first backfill."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "List due windows per endpoint WITHOUT calling Garmin—reads state "
            "from raw.poll_window and applies the same scheduling logic as real "
            "poll (garminator.scheduler.planning.due_windows), no side effects."
        ),
    )
    return parser


def _build_real_window_fetcher(
    settings: Settings, conn: DBConnection, tracker: IngestRunTracker
) -> WindowFetcher:
    """Production wiring: the only place in the repo that instantiates
    ``garminconnect.Garmin`` with real credentials and actually connects.

    Never invoked by a test in this batch—see the module docstring.
    """
    transport = garminconnect.Garmin(
        email=settings.garmin_username.get_secret_value(),
        password=settings.garmin_password.get_secret_value(),
    )
    client = GarminClient(
        transport=transport,
        tokenstore_path=settings.garmin_tokens_path,
        conn=conn,
    )
    client.login()
    return GarminWindowFetcher(client=client, tracker=tracker)


def _print_dry_run(
    conn: DBConnection,
    *,
    endpoints: list[str],
    history_start: date,
    today: date,
    refresh_lookback_days: int,
    max_windows_per_endpoint: int,
    now: datetime,
) -> None:
    """Print due windows per endpoint, never calling ``fetch_window``.

    Applies EXACTLY :func:`garminator.scheduler.planning.due_windows`—the same
    pure function :func:`~garminator.scheduler.service.run_scheduled_poll` uses
    internally—so the printed list truly matches what a real run would process,
    bounded by ``max_windows_per_endpoint`` included.
    """
    print(f"Catchup from {history_start} to {today} (dry-run, no Garmin calls):")
    for endpoint in endpoints:
        states = load_window_states(conn, endpoint=endpoint)
        due = due_windows(
            history_start=history_start,
            today=today,
            states=states,
            now=now,
            refresh_lookback_days=refresh_lookback_days,
        )
        treated = due[:max_windows_per_endpoint]
        remainder = len(due) - len(treated)
        dates_str = ", ".join(d.isoformat() for d in treated) if treated else "(none)"
        suffix = f" (+{remainder} deferred to next run)" if remainder > 0 else ""
        print(f"  {endpoint}: {len(due)} window(s) due → {dates_str}{suffix}")


def _print_summary(summary: RunSummary) -> None:
    endpoints = sorted({outcome.endpoint for outcome in summary.outcomes})
    for endpoint in endpoints:
        rows = [o for o in summary.outcomes if o.endpoint == endpoint]
        ok = [o for o in rows if o.succeeded]
        ko = [o for o in rows if not o.succeeded]
        new_payloads = sum(o.new_payload_count for o in ok)
        already_seen = sum(o.already_seen_count for o in ok)
        print(
            f"  {endpoint}: {len(rows)} window(s) processed, {len(ok)} succeeded "
            f"({new_payloads} new payload(s), {already_seen} already seen), "
            f"{len(ko)} failed"
        )
        for outcome in ko:
            print(f"    FAILED {outcome.window_date}: {outcome.error}")
    if summary.truncated_endpoints:
        print(
            "  Truncated endpoints this run (remainder picked up next run): "
            + ", ".join(summary.truncated_endpoints)
        )


def _reconcile_tracker_with_summary(tracker: IngestRunTracker, summary: RunSummary) -> None:
    """Force final ``ingest_run`` status to reflect ALL failed windows—including
    those whose errors never went through ``GarminClient.fetch`` (fix C3, see batch
    report).

    Verified fact: when ``GarminClient.fetch`` raises (network, authentication, rate
    limit...), it has already called ``tracker.record_error()`` before re-raising -- the
    tracker is then already correct, and ``ingest_run`` will already close as
    ``'partial'``. The ONLY real gap is an exception raised AFTER ``client.fetch``
    returns, inside ``garminator.scheduler.fetcher`` (malformed payload while extracting
    the ``activityId``): that window is marked ``failed`` by ``run_scheduled_poll``, but
    no ``record_error()`` was ever called for it.

    Rather than guessing WHICH of the failed windows corresponds to that gap (this module
    only has access to ``RunSummary``, not to the fetcher's internal execution trace), we
    compare the total number of failed windows to the number of errors already counted by
    the tracker and only fill the shortfall -- errors already counted by ``client.fetch``
    are therefore NEVER counted twice here.

    Accepted trade-off: ``IngestRunTracker.record_error()``
    (``garminator.garmin.client``, outside this scope) increments both ``errors`` AND
    ``requests`` -- there is no way to increment one without the other short of modifying
    ``client.py``. Filling the shortfall through ``record_error()`` therefore inflates
    ``requests`` very slightly (at most one unit per failed window not already counted).
    ``requests`` is only a diagnostic counter (never read for business logic, only written
    into ``raw.ingest_run.notes``): that minor flaw is strictly preferable to a ``status``
    staying ``'ok'`` while windows were lost.
    """
    missing = len(summary.failed) - tracker.errors
    for _ in range(missing):
        tracker.record_error()


def _validate_cli_args(args: argparse.Namespace) -> int | None:
    """Validate purely syntactic arguments, before any network or database access.

    Returns exit code to propagate, or ``None`` if all valid. Extracted from
    ``main()`` for two reasons: keep orchestration function under ruff's exit
    branch threshold (``PLR0911``), and group in one place rejections that must
    all cost code 2—unknown endpoint and nonsensical bound are the same error
    class, the caller's.

    Fix C5: unvalidated bounds traversed to ``due_windows`` (uncaught ``ValueError``
    → raw traceback) or produced silently pointless / surprisingly truncated run.
    """
    endpoints = args.endpoints if args.endpoints else list(DEFAULT_ENDPOINTS)
    unknown = [e for e in endpoints if not is_known_endpoint(e)]
    if unknown:
        print(f"Error: unknown endpoint(s) from catalog: {unknown}", file=sys.stderr)
        return 2

    if args.refresh_lookback_days is not None and args.refresh_lookback_days < 0:
        print(
            "Error: --refresh-lookback-days must be >= 0 " f"(got {args.refresh_lookback_days}).",
            file=sys.stderr,
        )
        return 2

    if args.max_windows_per_endpoint <= 0:
        print(
            "Error: --max-windows-per-endpoint must be > 0 "
            f"(got {args.max_windows_per_endpoint})—0 or negative produces "
            "a run that is either pointless or incoherently truncated.",
            file=sys.stderr,
        )
        return 2

    return None


def _downloader_of(fetch_window: WindowFetcher) -> OriginalFileDownloader | None:
    """Get the Garmin client carried by a :class:`WindowFetcher`, if it has one.

    Original file download deliberately reuses the poll's already-authenticated
    session: opening a second session multiplies SSO authentications, and a burst
    of auths is exactly what locks a Garmin account. ``GarminWindowFetcher``
    exposes its client as an attribute (``client``); access is structural rather
    than typed because ``build_window_fetcher`` is an injection point declared as
    returning plain ``WindowFetcher``, which tests satisfy with a bare function.

    Returns ``None`` if fetcher has no client—archiving is then simply skipped,
    never attempted with a rigged session. ``tests/scheduler/test_scheduler_main.py``
    verifies that the production fetcher does expose one: without this test, an
    attribute rename silently disables all archiving.
    """
    client = getattr(fetch_window, "client", None)
    if client is None or not hasattr(client, "download_original"):
        return None
    return cast("OriginalFileDownloader", client)


def _archive_activity_files(
    conn: DBConnection,
    *,
    summary: RunSummary,
    downloader: OriginalFileDownloader,
    archive_root: Path,
    today: date,
    tracker: IngestRunTracker,
) -> None:
    """Archive original files of activities this run just polled.

    ``activityId``s come from ``scope_key``s already returned by fetcher for
    ``get_activities_by_date``: no extra request is made to re-ask for a list
    the run just got.

    Only SUCCEEDED windows are processed: a failed window has no reliable activity
    list, and it will be replayed next run anyway. ``archive_activity_file``
    decides alone from ``raw.fit_file`` whether to download, refresh, or do
    nothing—a day already archived thus costs no network request next run.
    """
    for outcome in summary.succeeded:
        if outcome.endpoint != _ACTIVITIES_ENDPOINT or not outcome.scope_keys:
            continue
        day_summary = archive_activities_for_day(
            downloader,
            conn,
            activity_ids=outcome.scope_keys,
            window_date=outcome.window_date,
            today=today,
            archive_root=archive_root,
            dl_fmt=garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL,
            tracker=tracker,
        )
        for line in day_summary.as_log_lines():
            print(line)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    connect_db: Callable[[str], Any] = connect,
    build_window_fetcher: Callable[
        [Settings, DBConnection, IngestRunTracker], WindowFetcher
    ] = _build_real_window_fetcher,
) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)

    invalid = _validate_cli_args(args)
    if invalid is not None:
        return invalid

    endpoints = args.endpoints if args.endpoints else list(DEFAULT_ENDPOINTS)

    # Fix C2: ``--dry-run`` must require NO Garmin secrets. So resolve DSN like
    # ``garminator.ops.__main__`` (``resolve_dsn``, never building ``Settings``)
    # and only load ``Settings`` if truly needed—never in dry-run, unless caller
    # explicitly injected one (tests).
    try:
        dsn = args.dsn if args.dsn else resolve_dsn(None)
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    active_settings = settings
    if not args.dry_run and active_settings is None:
        try:
            active_settings = get_settings()
        except SettingsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    history_start = (
        args.history_start
        if args.history_start is not None
        else (
            active_settings.history_start
            if active_settings is not None
            else _FALLBACK_HISTORY_START
        )
    )
    today = args.today if args.today is not None else datetime.now(UTC).date()
    refresh_lookback_days = (
        args.refresh_lookback_days
        if args.refresh_lookback_days is not None
        else (
            active_settings.refresh_lookback_days
            if active_settings is not None
            else DEFAULT_REFRESH_LOOKBACK_DAYS
        )
    )
    now = datetime.now(UTC)

    conn = connect_db(dsn)
    try:
        if args.dry_run:
            _print_dry_run(
                conn,
                endpoints=endpoints,
                history_start=history_start,
                today=today,
                refresh_lookback_days=refresh_lookback_days,
                max_windows_per_endpoint=args.max_windows_per_endpoint,
                now=now,
            )
            return 0

        # ``active_settings`` guaranteed non-``None`` here: either injected by caller
        # or loaded via ``get_settings()`` just above (the only branch, outside
        # dry-run, where this module truly needs Garmin credentials). ``raise`` not
        # ``assert`` (banned by flake8-bandit/S101 outside tests): this would signal
        # a bug in this module, never invalid operator input.
        if active_settings is None:  # pragma: no cover—unreachable, see above
            raise RuntimeError(
                "internal bug: active_settings is None outside dry-run after resolution"
            )

        # Fix C1: 'scheduled' is not in ``ingest_run_mode_ck``
        # (migrations/002_raw.sql)—only 'daily' fits semantically for a daily
        # scheduled run among SQL constraint's accepted values.
        with ingest_run(conn, mode="daily", code_version=__version__) as tracker:
            fetch_window = build_window_fetcher(active_settings, conn, tracker)
            summary = run_scheduled_poll(
                conn,
                endpoints=endpoints,
                history_start=history_start,
                today=today,
                fetch_window=fetch_window,
                ingest_run_id=tracker.ingest_run_id,
                now=now,
                refresh_lookback_days=refresh_lookback_days,
                max_windows_per_endpoint=args.max_windows_per_endpoint,
            )
            if not args.no_activity_files:
                downloader = _downloader_of(fetch_window)
                if downloader is None:
                    logger.warning(
                        "Injected WindowFetcher exposes no Garmin client: "
                        "original file archiving skipped for this run."
                    )
                else:
                    _archive_activity_files(
                        conn,
                        summary=summary,
                        downloader=downloader,
                        archive_root=active_settings.fit_archive_path,
                        today=today,
                        tracker=tracker,
                    )

            _reconcile_tracker_with_summary(tracker, summary)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    _print_summary(summary)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
