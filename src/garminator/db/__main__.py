"""CLI du runner de migrations : ``python -m garminator.db <migrate|status> [--dsn ...]``.

Volontairement un simple ``argparse`` et non une entrée ``[project.scripts]`` : ce module
n'a pas la main sur ``pyproject.toml`` (hors périmètre du lot L1). Voir le rapport de fin
de lot pour ce qu'un futur lot pourrait y ajouter.
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
        description="Runner de migrations SQL garminator (lot L1).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "DSN PostgreSQL. À défaut, lu depuis la variable d'environnement "
            "GARMINATOR_POSTGRES_DSN."
        ),
    )
    parser.add_argument(
        "--migrations-dir",
        default=DEFAULT_MIGRATIONS_DIR,
        type=Path,
        help="Répertoire des fichiers de migration (défaut : migrations/ du dépôt).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Applique les migrations en attente.")
    subparsers.add_parser("status", help="Affiche l'état des migrations sans rien appliquer.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            applied = migrate(dsn=args.dsn, migrations_dir=args.migrations_dir)
            if applied:
                print(f"{len(applied)} migration(s) appliquée(s) :")
                for m in applied:
                    print(f"  [{m.version:03d}] {m.name}")
            else:
                print("Rien à appliquer : la base est déjà à jour.")
            return 0
        report = status(dsn=args.dsn, migrations_dir=args.migrations_dir)
        print(format_status(report))
        return 1 if report.drift else 0
    except MigrationError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
