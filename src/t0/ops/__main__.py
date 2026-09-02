"""Ops CLI: ``python -m t0.ops <refresh|seed-pmc|backfill-status|health|reconcile>``.

Same style as :mod:`t0.db.__main__`: a simple ``argparse`` (no entry in
``[project.scripts]`` -- ``pyproject.toml`` is out of scope for this batch), and DSN is
resolved by ``--dsn`` OR the environment variable ``T0_POSTGRES_DSN``, never both
at once or any third mechanism.

Note on ``--dsn``: passing the DSN as a command-line argument exposes it in plain text
in the process list (``ps``, ``/proc/<pid>/cmdline``, Kubernetes CronJob Processes tab)
since the PostgreSQL password is encoded in it. This is why
:func:`t0.db.runner.resolve_dsn` (reused as-is here) prefers the environment
variable, and why Helm manifests for this batch inject the DSN via
``secretKeyRef`` -> container environment variable rather than via a command argument.
``--dsn`` is still offered for occasional interactive use (operator workstation), where
exposure is less critical than in shared environments.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from t0.db.runner import MigrationError, connect, resolve_dsn
from t0.logging_setup import configure_logging
from t0.ops.backfill import DEFAULT_HISTORY_START_ISO, backfill_progress
from t0.ops.health import build_health_report, format_health_report
from t0.ops.pmc_seed import (
    PmcSeedError,
    compute_pmc_seed,
    format_pmc_seed,
    write_pmc_seed,
)
from t0.ops.reconcile import (
    DBConnection as ReconcileConnection,
)
from t0.ops.reconcile import (
    format_reconcile_report,
    reconcile_archive,
)
from t0.ops.refresh import refresh_all_mart_views
from t0.scheduler.planning import DEFAULT_REFRESH_LOOKBACK_DAYS

if TYPE_CHECKING:
    from t0.scheduler.store import DBConnection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m t0.ops",
        description="T0 ops tools (batch L10): refresh mart views, track "
        "backfill progress, health check.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL DSN. If not provided, read from environment variable "
            "T0_POSTGRES_DSN. Prefer the environment variable: a DSN "
            "passed here appears in plain text in the process list."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "refresh",
        help="Refresh all materialized views mart.* (in dependency order).",
    )

    seed_parser = subparsers.add_parser(
        "seed-pmc",
        help=(
            "Compute and store the PMC seed (core.pmc_seed). mart.pmc is EMPTY "
            "without it: run this once, after an import and a refresh."
        ),
    )
    seed_parser.add_argument(
        "--athlete-id",
        required=True,
        type=int,
        help="Athlete the seed is computed for (core.athlete.athlete_id).",
    )
    seed_parser.add_argument(
        "--seed-date",
        default=None,
        type=date.fromisoformat,
        help=(
            "Start of the 42-day window (default: first day carrying a known load, "
            "read from mart.daily_load). Days with no computable TSS are NOT a "
            "history start: they would drag the average down."
        ),
    )
    seed_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the seed without writing it.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-status",
        help="Show backfill progress per endpoint (read-only).",
    )
    backfill_parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        required=True,
        help=(
            "Endpoint to examine, by its garminconnect method name as stored in "
            "raw.poll_window.endpoint (repeatable: --endpoint get_activities_by_date "
            "--endpoint get_sleep_data ...). A partial name would match no rows and "
            "produce an empty report."
        ),
    )
    backfill_parser.add_argument(
        "--history-start",
        default=DEFAULT_HISTORY_START_ISO,
        type=date.fromisoformat,
        help=f"Backfill start date (default: {DEFAULT_HISTORY_START_ISO}).",
    )
    backfill_parser.add_argument(
        "--today",
        default=None,
        type=date.fromisoformat,
        help=(
            "Date to consider as today (default: actual current date). "
            "Useful for replaying a past report."
        ),
    )
    backfill_parser.add_argument(
        "--refresh-lookback-days",
        default=DEFAULT_REFRESH_LOOKBACK_DAYS,
        type=int,
        help=("Refresh sliding window, in days " f"(default: {DEFAULT_REFRESH_LOOKBACK_DAYS})."),
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help=(
            "Compare archive volume to raw.fit_file (read-only): rows without "
            "file (CRITICAL) and files without row (benign)."
        ),
    )
    reconcile_parser.add_argument(
        "--archive-root",
        required=True,
        type=Path,
        help=(
            "Archive volume root as MOUNTED in this container. "
            "Must be the same path as used at write time, otherwise all "
            "rows will be reported as 'file missing'."
        ),
    )
    reconcile_parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Recalculate sha256 for each file instead of comparing size. "
            "Rereads entire volume: reserve for suspected silent corruption."
        ),
    )

    subparsers.add_parser(
        "health",
        help=("Health check: last ingestion, failed/stuck windows, view freshness."),
    )
    return parser


def _run_seed_pmc(dsn: str, args: argparse.Namespace) -> int:
    """Kept out of :func:`main` only to keep that function readable, not because the
    command deserves special treatment."""
    conn = connect(dsn)
    try:
        seed = compute_pmc_seed(conn, args.athlete_id, seed_date=args.seed_date)
        if not args.dry_run:
            write_pmc_seed(conn, seed)
            conn.commit()
    except PmcSeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    prefix = "Would write" if args.dry_run else "Written"
    print(f"{prefix}: {format_pmc_seed(seed)}")
    if not args.dry_run:
        print("Next: refresh mart.pmc with `python -m t0.ops refresh`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        dsn = resolve_dsn(args.dsn)
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.command == "refresh":
        conn = connect(dsn)
        conn.autocommit = True
        try:
            outcomes = refresh_all_mart_views(conn)
        finally:
            conn.close()
        failed = [o for o in outcomes if not o.succeeded]
        for outcome in outcomes:
            state = "OK" if outcome.succeeded else f"FAILED: {outcome.error}"
            print(f"  {outcome.step.view} ({outcome.step.mode}) -- {state}")
        return 1 if failed else 0

    if args.command == "seed-pmc":
        return _run_seed_pmc(dsn, args)

    if args.command == "backfill-status":
        conn = connect(dsn)
        try:
            today = args.today if args.today is not None else datetime.now().astimezone().date()
            progress = backfill_progress(
                cast("DBConnection", conn),
                endpoints=args.endpoints,
                history_start=args.history_start,
                today=today,
                refresh_lookback_days=args.refresh_lookback_days,
            )
        finally:
            conn.close()
        print(f"Backfill from {progress.history_start} to {progress.today}:")
        for ep in progress.by_endpoint:
            status = "up to date" if ep.is_caught_up else f"{ep.due} due window(s)"
            print(
                f"  {ep.endpoint}: {ep.done}/{ep.total_days} days done, {status} "
                f"(never_attempted={ep.never_attempted}, failed={ep.failed}, "
                f"stuck={ep.stale_in_progress}, oldest_due={ep.oldest_due})"
            )
        return 0 if progress.fully_caught_up else 1

    if args.command == "reconcile":
        conn = connect(dsn)
        try:
            # psycopg exposes overloaded cursor() variants; the reconciliation operation
            # only needs the smaller read-only protocol accepted by its implementation.
            reconcile_report = reconcile_archive(
                cast(ReconcileConnection, conn), args.archive_root, deep=args.deep
            )
        finally:
            conn.close()
        print(format_reconcile_report(reconcile_report))
        return 0 if reconcile_report.is_clean else 1

    conn = connect(dsn)
    try:
        report = build_health_report(conn)
    finally:
        conn.close()
    print(format_health_report(report))
    return 1 if (report.failed_windows or report.stuck_windows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
