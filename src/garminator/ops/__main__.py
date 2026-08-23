"""CLI d'exploitation : ``python -m garminator.ops <refresh|backfill-status|health|reconcile>``.

Même style que :mod:`garminator.db.__main__` : un simple ``argparse`` (pas d'entrée
``[project.scripts]`` -- ``pyproject.toml`` est hors périmètre de ce lot), et le DSN est
résolu par ``--dsn`` OU la variable d'environnement ``GARMINATOR_POSTGRES_DSN``, jamais
les deux en même temps ni un troisième mécanisme.

Note sur ``--dsn`` : passer le DSN en argument de ligne de commande l'expose en clair
dans la liste des processus (``ps``, ``/proc/<pid>/cmdline``, l'onglet Processus d'un
CronJob Kubernetes) puisque le mot de passe PostgreSQL y est encodé. C'est pourquoi
:func:`garminator.db.runner.resolve_dsn` (réutilisé ici tel quel) préfère la variable
d'environnement, et pourquoi les manifests Helm de ce lot injectent le DSN via
``secretKeyRef`` -> variable d'environnement du conteneur plutôt que via un argument de
commande. ``--dsn`` reste proposé pour un usage interactif ponctuel (poste de
l'opérateur), où l'exposition est moins sensible qu'en environnement partagé.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from garminator.db.runner import MigrationError, connect, resolve_dsn
from garminator.logging_setup import configure_logging
from garminator.ops.backfill import DEFAULT_HISTORY_START_ISO, backfill_progress
from garminator.ops.health import build_health_report, format_health_report
from garminator.ops.reconcile import format_reconcile_report, reconcile_archive
from garminator.ops.refresh import refresh_all_mart_views
from garminator.scheduler.planning import DEFAULT_REFRESH_LOOKBACK_DAYS

if TYPE_CHECKING:
    # Importé uniquement pour le `cast` ci-dessous : `DBConnection` est un protocole,
    # il n'a aucune existence à l'exécution.
    from garminator.scheduler.store import DBConnection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.ops",
        description="Outils d'exploitation garminator (lot L10) : rafraîchissement des "
        "vues mart, suivi du rattrapage historique, bilan de santé.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "DSN PostgreSQL. À défaut, lu depuis la variable d'environnement "
            "GARMINATOR_POSTGRES_DSN. Préférez la variable d'environnement : un DSN "
            "passé ici apparaît en clair dans la liste des processus."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "refresh",
        help="Rafraîchit toutes les vues matérialisées mart.* (ordre de dépendance imposé).",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-status",
        help="Affiche la progression du rattrapage historique par endpoint (lecture seule).",
    )
    backfill_parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        required=True,
        help=(
            "Endpoint à examiner, sous son nom de méthode garminconnect tel que stocké "
            "dans raw.poll_window.endpoint (répétable : --endpoint get_activities_by_date "
            "--endpoint get_sleep_data ...). Un nom abrégé ne correspondrait à aucune "
            "ligne et produirait un rapport vide."
        ),
    )
    backfill_parser.add_argument(
        "--history-start",
        default=DEFAULT_HISTORY_START_ISO,
        type=date.fromisoformat,
        help=f"Date de début du rattrapage historique (défaut : {DEFAULT_HISTORY_START_ISO}).",
    )
    backfill_parser.add_argument(
        "--today",
        default=None,
        type=date.fromisoformat,
        help=(
            "Date du jour à considérer (défaut : date du jour réelle). "
            "Utile pour rejouer un bilan passé."
        ),
    )
    backfill_parser.add_argument(
        "--refresh-lookback-days",
        default=DEFAULT_REFRESH_LOOKBACK_DAYS,
        type=int,
        help=(
            "Fenêtre glissante de rafraîchissement, en jours "
            f"(défaut : {DEFAULT_REFRESH_LOOKBACK_DAYS})."
        ),
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help=(
            "Compare le PVC d'archivage a raw.fit_file (lecture seule) : lignes sans "
            "fichier (GRAVE) et fichiers sans ligne (benin)."
        ),
    )
    reconcile_parser.add_argument(
        "--archive-root",
        required=True,
        type=Path,
        help=(
            "Racine du volume d'archivage, telle qu'elle est MONTÉE dans ce conteneur. "
            "Doit être le même chemin que celui utilisé à l'écriture, sans quoi toutes "
            "les lignes seront rapportées « fichier absent »."
        ),
    )
    reconcile_parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Recalcule le sha256 de chaque fichier au lieu de comparer sa taille. "
            "Relit tout le volume : à réserver à un soupçon de corruption silencieuse."
        ),
    )

    subparsers.add_parser(
        "health",
        help="Bilan de santé : dernière ingestion, fenêtres en échec/bloquées, fraîcheur mart.",
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

    if args.command == "refresh":
        conn = connect(dsn)
        conn.autocommit = True
        try:
            outcomes = refresh_all_mart_views(conn)
        finally:
            conn.close()
        failed = [o for o in outcomes if not o.succeeded]
        for outcome in outcomes:
            state = "OK" if outcome.succeeded else f"ÉCHEC : {outcome.error}"
            print(f"  {outcome.step.view} ({outcome.step.mode}) -- {state}")
        return 1 if failed else 0

    if args.command == "backfill-status":
        conn = connect(dsn)
        try:
            today = args.today if args.today is not None else datetime.now().date()
            progress = backfill_progress(
                # `DBConnection` est un protocole structurel volontairement minimal
                # (`cursor()` sans argument), fait pour injecter une doublure en test.
                # Une vraie `psycopg.Connection` le satisfait à l'exécution, mais mypy
                # refuse de rapprocher sa méthode `cursor` SURCHARGÉE d'un protocole qui
                # ne l'est pas. Élargir le protocole reviendrait à y recopier les quatre
                # surcharges de psycopg, ce qui lui ferait perdre sa raison d'être.
                cast("DBConnection", conn),
                endpoints=args.endpoints,
                history_start=args.history_start,
                today=today,
                refresh_lookback_days=args.refresh_lookback_days,
            )
        finally:
            conn.close()
        print(f"Rattrapage depuis {progress.history_start} jusqu'à {progress.today} :")
        for ep in progress.by_endpoint:
            statut = "à jour" if ep.is_caught_up else f"{ep.due} fenêtre(s) due(s)"
            print(
                f"  {ep.endpoint} : {ep.done}/{ep.total_days} jour(s) faits, {statut} "
                f"(jamais tentées={ep.never_attempted}, échec={ep.failed}, "
                f"bloquées={ep.stale_in_progress}, plus ancienne due={ep.oldest_due})"
            )
        return 0 if progress.fully_caught_up else 1

    if args.command == "reconcile":
        conn = connect(dsn)
        try:
            reconcile_report = reconcile_archive(conn, args.archive_root, deep=args.deep)
        finally:
            conn.close()
        print(format_reconcile_report(reconcile_report))
        # Code de sortie sur `is_clean` seul : un fichier orphelin ne doit PAS faire
        # échouer un CronJob, sous peine d'apprendre à ignorer ses alertes.
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
