"""CLI for migration runner: ``python -m garminator.db <migrate|status> [--dsn ...]``.

Deliberately plain ``argparse``, not a ``[project.scripts]`` entry: this module has no
control over ``pyproject.toml`` (out of scope for L1 batch). See batch end report for
what a future batch could add there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from garminator.db.runner import (
    DEFAULT_MIGRATIONS_DIR,
    MigrationError,
    format_status,
    migrate,
    status,
)
from garminator.logging_setup import configure_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.db",
        description="Garminator SQL migration runner (L1 batch).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL DSN. If omitted, read from GARMINATOR_POSTGRES_DSN environment " "variable."
        ),
    )
    parser.add_argument(
        "--migrations-dir",
        default=DEFAULT_MIGRATIONS_DIR,
        type=Path,
        help="Directory of migration files (default: migrations/ from repository).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply pending migrations.")
    subparsers.add_parser("status", help="Show migration status without applying anything.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            applied = migrate(dsn=args.dsn, migrations_dir=args.migrations_dir)
            if applied:
                print(f"{len(applied)} migration(s) applied:")
                for m in applied:
                    print(f"  [{m.version:03d}] {m.name}")
            else:
                print("Nothing to apply: database is already up to date.")
            return 0
        report = status(dsn=args.dsn, migrations_dir=args.migrations_dir)
        print(format_status(report))
        return 1 if report.drift else 0
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
