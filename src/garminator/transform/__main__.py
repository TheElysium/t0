"""CLI de transformation : ``python -m garminator.transform [--limit N] [--retry-failed]``.

Même patron que :mod:`garminator.ops.__main__` et :mod:`garminator.db.__main__` : un
``argparse`` sans dépendance, un DSN résolu par ``--dsn`` OU la variable
d'environnement ``GARMINATOR_POSTGRES_DSN``, et ``configure_logging()`` en toute
première instruction pour que même une erreur de configuration soit horodatée.

Rappel sur ``--dsn``, identique à celui d'``ops`` : un DSN passé en argument apparaît en
clair dans la liste des processus, mot de passe compris. Les manifests injectent donc la
variable d'environnement depuis un ``secretKeyRef`` ; ``--dsn`` n'est là que pour un
usage interactif sur le poste de l'opérateur.

CODES DE SORTIE, ET POURQUOI CE DÉCOUPAGE.

- ``0`` : passage nominal, y compris avec des fichiers REPORTÉS. Un fichier dont le
  résumé Garmin n'est pas encore ingéré est un état transitoire attendu ; le compter
  comme un échec ferait échouer le CronJob presque chaque nuit, et un CronJob qui échoue
  toutes les nuits est un CronJob qu'on cesse de regarder.
- ``1`` : au moins un fichier en échec. L'incident est déjà consigné dans
  ``raw.fit_file.parse_error`` et le code de sortie sert uniquement à le rendre visible
  sans lire les logs.
- ``2`` : le passage n'a pas pu commencer (DSN absent, base injoignable,
  ``core.athlete`` non peuplée ou ambiguë).
  Distinct de ``1`` parce que rien n'a été tenté : c'est une erreur de configuration, pas
  une donnée fautive.
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
    transform_pending_files,
)

if TYPE_CHECKING:
    # Importé pour le seul `cast` ci-dessous : `DBConnection` est un protocole, il n'a
    # aucune existence à l'exécution.
    from garminator.transform.runner import DBConnection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.transform",
        description=(
            "Transforme les fichiers .FIT/.TCX archivés et non encore parsés "
            "(raw.fit_file) en lignes core.* exploitables par les vues mart et Grafana."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "DSN PostgreSQL. À défaut, lu depuis GARMINATOR_POSTGRES_DSN. Préférez la "
            "variable d'environnement : un DSN passé ici apparaît en clair dans la "
            "liste des processus."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_LIMIT,
        help=(
            f"Nombre maximal de fichiers traités par passage (défaut {DEFAULT_BATCH_LIMIT}). "
            "Plusieurs passages courts valent mieux qu'un long qu'une éviction de pod "
            "ramènerait à zéro."
        ),
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=None,
        help=(
            "Athlète auquel rattacher les activités. À défaut, l'unique ligne de "
            "core.athlete ; si la table est vide ou en contient plusieurs, le passage "
            "s'arrête plutôt que de deviner."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Rejoue aussi les fichiers portant déjà un parse_error. À n'utiliser "
            "qu'APRÈS avoir corrigé la cause (correspondance de sport manquante, "
            "décodeur bugué) : sans cela ils échoueraient à l'identique."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        dsn = resolve_dsn(args.dsn)
    except MigrationError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    try:
        conn = connect(dsn)
    except psycopg.OperationalError as exc:
        # Base injoignable (redemarrage, PVC pas encore monte, mot de passe tourne).
        # Sans cette capture l'exception remonte et Python sort en 1, que l'exploitant
        # lit comme "un fichier a echoue" -- il chercherait un fichier fautif qui
        # n'existe pas, alors qu'aucun fichier n'a ete touche.
        print(f"Erreur : connexion a PostgreSQL impossible -- {exc}", file=sys.stderr)
        return 2

    try:
        try:
            report = transform_pending_files(
                # Protocole structurel volontairement minimal, fait pour injecter une
                # doublure en test. Une vraie `psycopg.Connection` le satisfait à
                # l'exécution, mais mypy refuse de rapprocher sa méthode `cursor`
                # SURCHARGÉE d'un protocole qui ne l'est pas ; élargir le protocole
                # reviendrait à y recopier les surcharges de psycopg, ce qui lui ferait
                # perdre sa raison d'être.
                cast("DBConnection", conn),
                athlete_id=args.athlete_id,
                limit=args.limit,
                retry_failed=args.retry_failed,
            )
        except ValueError as exc:
            # Typiquement core.athlete vide ou ambiguë : rien n'a été tenté.
            print(f"Erreur : {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    print(format_transform_report(report))
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover -- point d'entrée
    raise SystemExit(main())
