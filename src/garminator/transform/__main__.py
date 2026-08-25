"""Transform CLI: ``python -m garminator.transform [--limit N] [--retry-failed]``.

TWO PASSES, ONE COMMAND. The file pass (``raw.fit_file`` -> activities) and the wellness
pass (``raw.api_response`` -> sleep, HRV, stress, Body Battery) run one after the other
here rather than under two commands. They share nothing but the connection, but they are
the same step of the operator's chain -- ``scheduler`` then ``transform`` then ``ops
refresh`` -- and splitting them would mean a second CronJob whose only distinguishing
feature is that forgetting it leaves the wellness dashboards empty with no error, which is
precisely the failure this pass was written to fix.

Same pattern as :mod:`garminator.ops.__main__` and :mod:`garminator.db.__main__`: a
dependency-free ``argparse``, a DSN resolved from ``--dsn`` OR the
``GARMINATOR_POSTGRES_DSN`` environment variable, and ``configure_logging()`` as the
very first statement so that even a configuration error is timestamped.

Same ``--dsn`` caveat as in ``ops``: a DSN passed as an argument shows up in clear text
in the process list, password included. Manifests therefore inject the environment
variable from a ``secretKeyRef``; ``--dsn`` exists only for interactive use on the
operator's machine.

EXIT CODES, AND WHY THEY ARE SPLIT THIS WAY.

- ``0``: nominal pass, including DEFERRED files. A file whose Garmin summary is not
  ingested yet is an expected transient state; counting it as a failure would fail the
  CronJob almost every night, and a CronJob that fails every night is a CronJob nobody
  looks at any more.
- ``1``: at least one file failed. The incident is already recorded in
  ``raw.fit_file.parse_error``; the exit code only makes it visible without reading the
  logs.
- ``2``: the pass could not start (missing DSN, unreachable database, ``core.athlete``
  empty or ambiguous). Distinct from ``1`` because nothing was attempted: this is a
  configuration error, not bad data.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, cast

import psycopg

from garminator.db.runner import MigrationError, connect, resolve_dsn
from garminator.logging_setup import configure_logging
from garminator.transform.runner import (
    DEFAULT_BATCH_LIMIT,
    format_transform_report,
    resolve_athlete_id,
    transform_pending_files,
)
from garminator.transform.wellness_runner import (
    DEFAULT_DAY_LIMIT,
    format_wellness_report,
    transform_wellness_days,
)

if TYPE_CHECKING:
    # Imported solely for the `cast` below: `DBConnection` is a protocol with no
    # runtime existence.
    from garminator.transform.runner import DBConnection
    from garminator.transform.wellness_runner import DBConnection as WellnessDBConnection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.transform",
        description=(
            "Transform archived, not-yet-parsed .FIT/.TCX files (raw.fit_file) into "
            "core.* rows usable by the mart views and Grafana."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL DSN. Otherwise read from GARMINATOR_POSTGRES_DSN. Prefer the "
            "environment variable: a DSN passed here shows up in clear text in the "
            "process list."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_LIMIT,
        help=(
            f"Maximum number of files handled per pass (default {DEFAULT_BATCH_LIMIT}). "
            "Several short passes beat one long pass that a pod eviction would reset to "
            "zero."
        ),
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=None,
        help=(
            "Athlete the activities are attached to. Otherwise the single core.athlete "
            "row; if the table is empty or holds several, the pass stops rather than "
            "guessing."
        ),
    )
    parser.add_argument(
        "--wellness-limit",
        type=int,
        default=DEFAULT_DAY_LIMIT,
        help=(
            f"Maximum number of local days handled by the wellness pass (default "
            f"{DEFAULT_DAY_LIMIT}). Unchanged days are skipped by content hash, so this "
            "bounds a backfill, not a nightly run."
        ),
    )
    parser.add_argument(
        "--force-wellness",
        action="store_true",
        help=(
            "Reprocess every wellness day even when its payloads are unchanged. Needed "
            "after changing the extraction code itself, which no content hash can detect."
        ),
    )
    parser.add_argument(
        "--skip-wellness",
        action="store_true",
        help="Run the file pass only, leaving raw.api_response untouched.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Also replay files that already carry a parse_error. Use only AFTER fixing "
            "the cause (missing sport mapping, buggy decoder): otherwise they fail "
            "identically."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        dsn = resolve_dsn(args.dsn)
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        conn = connect(dsn)
    except psycopg.OperationalError as exc:
        # Database unreachable (restart, PVC not mounted yet, password rotated).
        # Without this catch the exception propagates and Python exits 1, which the
        # operator reads as "a file failed" -- they would hunt for a faulty file that
        # does not exist, when in fact no file was touched at all.
        print(f"Error: cannot connect to PostgreSQL -- {exc}", file=sys.stderr)
        return 2

    try:
        try:
            athlete_id = (
                args.athlete_id
                if args.athlete_id is not None
                else resolve_athlete_id(cast("DBConnection", conn))
            )
            report = transform_pending_files(
                # Deliberately minimal structural protocol, meant to let tests inject a
                # double. A real `psycopg.Connection` satisfies it at runtime, but mypy
                # refuses to match its OVERLOADED `cursor` method against a protocol that
                # is not; widening the protocol would mean copying psycopg's overloads
                # into it, defeating its purpose.
                cast("DBConnection", conn),
                athlete_id=athlete_id,
                limit=args.limit,
                retry_failed=args.retry_failed,
            )
            wellness = (
                None
                if args.skip_wellness
                else transform_wellness_days(
                    cast("WellnessDBConnection", conn),
                    athlete_id=athlete_id,
                    limit=args.wellness_limit,
                    force=args.force_wellness,
                )
            )
        except ValueError as exc:
            # Typically core.athlete empty or ambiguous: nothing was attempted.
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    print(format_transform_report(report))
    if wellness is not None:
        print(format_wellness_report(wellness))
    failed = report.failed or (wellness.failed if wellness is not None else 0)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover -- entry point
    raise SystemExit(main())
