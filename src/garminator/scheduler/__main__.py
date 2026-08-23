"""Point d'entrée du service planifié : ``python -m garminator.scheduler [options]``.

C'est ce module -- et lui seul -- qui a le droit de construire un vrai transport
``garminconnect.Garmin`` : tout le reste du paquet (``planning``, ``store``, ``service``,
``catalog``, ``fetcher``) reste testable sans réseau parce qu'il ne connaît que des
protocoles injectés (:class:`~garminator.scheduler.service.WindowFetcher`,
:class:`~garminator.scheduler.store.DBConnection`). C'est le rôle exact que
``client.py`` (lot L2) et ``service.py`` (lot L6) documentaient tous deux comme laissé à
un futur point d'entrée -- celui-ci.

Même style que :mod:`garminator.ops.__main__` et :mod:`garminator.db.__main__` : un
simple ``argparse``, un ``--dsn`` optionnel (préférer la variable d'environnement en
production, l'argument expose le mot de passe dans la liste des processus), un code de
sortie non nul dès qu'il y a matière à alerter un opérateur -- ici, dès qu'au moins une
fenêtre a échoué. Un ``CronJob`` Kubernetes qui sort toujours ``0`` ne fait jamais
remonter d'échec de sondage : c'est précisément ce que ce module évite.

``main`` accepte des points d'injection (``settings``, ``connect_db``,
``build_window_fetcher``) à seule fin de test : la valeur par défaut de chacun est le
comportement de production réel, mais aucun test de ce lot n'exerce ces valeurs par
défaut -- ils injectent systématiquement des doublures, pour ne jamais construire de
vrai transport ``garminconnect`` ni ouvrir de vraie connexion PostgreSQL.
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

#: Seul endpoint du catalogue à produire des ``scope_key`` = ``activityId``
#: (``CallShape.ACTIVITIES_BY_DATE``), donc le seul par lequel ce module sait quels
#: fichiers originaux archiver.
_ACTIVITIES_ENDPOINT = "get_activities_by_date"

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from garminator.garmin.client import IngestRunTracker
    from garminator.scheduler.activity_files import OriginalFileDownloader
    from garminator.scheduler.service import RunSummary, WindowFetcher
    from garminator.scheduler.store import DBConnection

#: Repli utilisé pour ``history_start`` quand ni ``--history-start`` ni des
#: ``Settings`` chargés ne sont disponibles -- notamment en ``--dry-run`` sans aucune
#: variable ``GARMINATOR_*`` définie (correction C2 : le dry-run ne doit dépendre
#: d'aucun secret Garmin). Valeur identique à ``Settings.history_start``
#: (``garminator.settings``) et à ``garminator.ops.backfill.DEFAULT_HISTORY_START_ISO`` --
#: dupliquée plutôt qu'importée pour la même raison que ces deux-là se dupliquent déjà
#: entre elles : ce module doit pouvoir fonctionner (en dry-run) sans jamais construire
#: de ``Settings``, donc sans dépendre de ``garminator.settings`` pour cette seule
#: constante ; et il ne doit pas non plus dépendre de ``garminator.ops`` pour un module
#: qui n'a par ailleurs aucune raison de connaître ce paquet d'exploitation.
#: ``tests/test_settings.py`` verrouille déjà l'égalité entre les deux premières ; ce
#: module ne peut pas se tester lui-même contre elles sans réintroduire la dépendance
#: qu'il évite -- toute dérive future devra être détectée à l'œil lors d'une revue.
_FALLBACK_HISTORY_START = date(2025, 4, 3)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garminator.scheduler",
        description=(
            "Sondage périodique planifié (lot L11) : rattrape l'historique puis "
            "maintient à jour les fenêtres [endpoint, jour] via garminconnect, sans "
            "jamais perdre une fenêtre en cas d'échec partiel."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "DSN PostgreSQL. À défaut, lu depuis GARMINATOR_POSTGRES_DSN (via la "
            "configuration applicative). Préférez la variable d'environnement : un DSN "
            "passé ici apparaît en clair dans la liste des processus."
        ),
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        default=None,
        help=(
            "Endpoint garminconnect à sonder (répétable : --endpoint get_sleep_data "
            "--endpoint get_hrv_data ...). Défaut : le sous-ensemble pertinent pour un "
            "triathlète (garminator.scheduler.catalog.DEFAULT_ENDPOINTS)."
        ),
    )
    parser.add_argument(
        "--today",
        default=None,
        type=date.fromisoformat,
        help="Date du jour à considérer (défaut : date du jour réelle, UTC).",
    )
    parser.add_argument(
        "--history-start",
        default=None,
        type=date.fromisoformat,
        help=(
            "Date de début du rattrapage historique (défaut : "
            "GARMINATOR_HISTORY_START, ou 2025-04-03 à défaut de configuration)."
        ),
    )
    parser.add_argument(
        "--refresh-lookback-days",
        default=None,
        type=int,
        help=(
            "Fenêtre glissante de rafraîchissement, en jours (défaut : "
            "GARMINATOR_REFRESH_LOOKBACK_DAYS, ou 3 à défaut de configuration)."
        ),
    )
    parser.add_argument(
        "--max-windows-per-endpoint",
        default=DEFAULT_MAX_WINDOWS_PER_ENDPOINT,
        type=int,
        help=(
            "Borne de fenêtres traitées par endpoint et par run (défaut : "
            f"{DEFAULT_MAX_WINDOWS_PER_ENDPOINT})."
        ),
    )
    parser.add_argument(
        "--no-activity-files",
        action="store_true",
        help=(
            "Ne télécharge PAS les fichiers originaux (.FIT/.TCX) des activités sondées. "
            "Par défaut ils sont archivés sur le PVC juste après le sondage : c'est la "
            "seule source des données seconde-par-seconde, que l'API JSON ne fournit pas. "
            "N'utiliser que pour un run de rattrapage purement JSON, ou pour éviter la "
            "salve de téléchargements du tout premier backfill."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Liste les fenêtres dues par endpoint SANS appeler Garmin -- lit l'état "
            "persisté dans raw.poll_window et applique la même logique de "
            "planification que le sondage réel (garminator.scheduler.planning."
            "due_windows), sans effet de bord."
        ),
    )
    return parser


def _build_real_window_fetcher(
    settings: Settings, conn: DBConnection, tracker: IngestRunTracker
) -> WindowFetcher:
    """Câblage de production : le seul endroit du dépôt qui instancie
    ``garminconnect.Garmin`` avec de vrais identifiants et se connecte réellement.

    N'est jamais invoqué par un test de ce lot -- voir le docstring du module.
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
    """Affiche les fenêtres dues par endpoint, sans jamais appeler ``fetch_window``.

    Applique EXACTEMENT :func:`garminator.scheduler.planning.due_windows` -- la même
    fonction pure que :func:`~garminator.scheduler.service.run_scheduled_poll` utilise
    en interne -- pour que la liste affichée corresponde vraiment à ce qu'un run réel
    traiterait, borne par ``max_windows_per_endpoint`` incluse.
    """
    print(f"Rattrapage depuis {history_start} jusqu'à {today} (dry-run, aucun appel Garmin) :")
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
        dates_str = ", ".join(d.isoformat() for d in treated) if treated else "(aucune)"
        suffix = f" (+{remainder} reportée(s) au run suivant)" if remainder > 0 else ""
        print(f"  {endpoint} : {len(due)} fenêtre(s) due(s) -> {dates_str}{suffix}")


def _print_summary(summary: RunSummary) -> None:
    endpoints = sorted({outcome.endpoint for outcome in summary.outcomes})
    for endpoint in endpoints:
        rows = [o for o in summary.outcomes if o.endpoint == endpoint]
        ok = [o for o in rows if o.succeeded]
        ko = [o for o in rows if not o.succeeded]
        new_payloads = sum(o.new_payload_count for o in ok)
        already_seen = sum(o.already_seen_count for o in ok)
        print(
            f"  {endpoint} : {len(rows)} fenêtre(s) traitée(s), {len(ok)} réussie(s) "
            f"({new_payloads} payload(s) nouveau(x), {already_seen} déjà vu(s)), "
            f"{len(ko)} en échec"
        )
        for outcome in ko:
            print(f"    ÉCHEC {outcome.window_date} : {outcome.error}")
    if summary.truncated_endpoints:
        print(
            "  Endpoints tronqués ce run (reliquat repris au run suivant) : "
            + ", ".join(summary.truncated_endpoints)
        )


def _reconcile_tracker_with_summary(tracker: IngestRunTracker, summary: RunSummary) -> None:
    """Force le statut final d'``ingest_run`` à refléter TOUTE fenêtre en échec -- y
    compris celles dont l'erreur n'a jamais transité par ``GarminClient.fetch`` (correction
    C3, cf. rapport de lot).

    Constat vérifié : quand ``GarminClient.fetch`` lève (réseau, authentification,
    débit...), il a déjà appelé ``tracker.record_error()`` avant de re-lever -- le
    tracker est alors déjà correct, et ``ingest_run`` clôturera déjà en ``'partial'``.
    Le SEUL trou réel est l'exception levée APRÈS le retour de ``client.fetch``, dans
    ``garminator.scheduler.fetcher`` (payload mal formé lors de l'extraction des
    ``activityId``) : cette fenêtre est marquée ``failed`` par ``run_scheduled_poll``,
    mais aucun ``record_error()`` n'a jamais été appelé pour elle.

    Plutôt que de deviner LAQUELLE des fenêtres en échec correspond à ce trou (ce module
    n'a accès qu'à ``RunSummary``, pas à la trace d'exécution interne du fetcher), on
    compare le nombre total de fenêtres en échec au nombre d'erreurs déjà comptées par
    le tracker et on ne comble que le manque -- les erreurs déjà comptées par
    ``client.fetch`` ne sont donc JAMAIS recomptées ici.

    Compromis assumé (arbitrage documenté dans le rapport de lot) :
    ``IngestRunTracker.record_error()`` (``garminator.garmin.client``, hors périmètre de
    ce lot) incrémente à la fois ``errors`` ET ``requests`` -- il n'existe aucun moyen
    d'incrémenter l'un sans l'autre sans modifier ``client.py``. Combler le manque via
    ``record_error()`` gonfle donc très légèrement ``requests`` (au plus une unité par
    fenêtre en échec non déjà comptée). ``requests`` n'est qu'un compteur diagnostique
    (jamais lu pour du calcul métier, seulement écrit dans ``raw.ingest_run.notes``) :
    ce vice mineur est strictement préférable à un ``status`` qui reste ``'ok'`` alors
    que des fenêtres ont été perdues.
    """
    missing = len(summary.failed) - tracker.errors
    for _ in range(missing):
        tracker.record_error()


def _validate_cli_args(args: argparse.Namespace) -> int | None:
    """Valide les arguments purement syntaxiques, avant tout accès réseau ou base.

    Retourne le code de sortie à propager, ou ``None`` si tout est valide. Extrait de
    ``main()`` pour deux raisons : garder la fonction d'orchestration sous le seuil de
    branches de sortie que ruff impose (``PLR0911``), et regrouper au même endroit les
    refus qui doivent tous coûter un code 2 -- un endpoint absent du catalogue et une
    borne absurde sont la même classe d'erreur, celle de l'appelant.

    Correction C5 : des bornes non validées traversaient jusqu'à ``due_windows``
    (``ValueError`` non rattrapée -> traceback brut) ou produisaient un run
    silencieusement inutile / tronqué de façon surprenante.
    """
    endpoints = args.endpoints if args.endpoints else list(DEFAULT_ENDPOINTS)
    unknown = [e for e in endpoints if not is_known_endpoint(e)]
    if unknown:
        print(f"Erreur : endpoint(s) inconnu(s) du catalogue : {unknown}", file=sys.stderr)
        return 2

    if args.refresh_lookback_days is not None and args.refresh_lookback_days < 0:
        print(
            "Erreur : --refresh-lookback-days doit être >= 0 "
            f"(reçu {args.refresh_lookback_days}).",
            file=sys.stderr,
        )
        return 2

    if args.max_windows_per_endpoint <= 0:
        print(
            "Erreur : --max-windows-per-endpoint doit être > 0 "
            f"(reçu {args.max_windows_per_endpoint}) -- 0 ou une valeur négative produit "
            "un run soit totalement inutile, soit tronqué de façon incohérente.",
            file=sys.stderr,
        )
        return 2

    return None


def _downloader_of(fetch_window: WindowFetcher) -> OriginalFileDownloader | None:
    """Récupère le client Garmin porté par un :class:`WindowFetcher`, s'il en porte un.

    Le téléchargement des fichiers originaux réutilise délibérément la session déjà
    authentifiée du sondage : ouvrir une seconde session multiplierait les
    authentifications SSO, et une salve d'authentifications est précisément ce qui fait
    bloquer un compte Garmin. ``GarminWindowFetcher`` expose son client en attribut
    (``client``) ; l'accès est structurel plutôt que typé parce que ``build_window_fetcher``
    est un point d'injection déclaré comme retournant un simple ``WindowFetcher``, que les
    tests satisfont avec une fonction nue.

    Retourne ``None`` si le fetcher ne porte pas de client -- l'archivage est alors
    simplement sauté, jamais tenté avec une session bricolée.
    ``tests/scheduler/test_scheduler_main.py`` vérifie que le fetcher de production, lui,
    en expose bien un : sans ce test, un renommage d'attribut désactiverait silencieusement
    tout l'archivage.
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
    """Archive les fichiers originaux des activités que ce run vient de sonder.

    Les ``activityId`` proviennent des ``scope_key`` déjà rapportés par le fetcher pour
    ``get_activities_by_date`` : aucune requête supplémentaire n'est faite pour
    redemander une liste que le run vient d'obtenir.

    Seules les fenêtres RÉUSSIES sont traitées : une fenêtre en échec n'a pas de liste
    d'activités fiable, et elle sera de toute façon rejouée au run suivant.
    ``archive_activity_file`` décide seul, à partir de ``raw.fit_file``, s'il faut
    télécharger, rafraîchir ou ne rien faire -- un jour déjà archivé ne coûte donc aucune
    requête réseau au run suivant.
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

    # Correction C2 : ``--dry-run`` ne doit exiger AUCUN secret Garmin. On résout donc le
    # DSN comme ``garminator.ops.__main__`` (``resolve_dsn``, sans jamais construire de
    # ``Settings``) et on ne charge ``Settings`` que si on en a réellement besoin --
    # jamais en dry-run, sauf si l'appelant en a explicitement injecté un (tests).
    try:
        dsn = args.dsn if args.dsn else resolve_dsn(None)
    except MigrationError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    active_settings = settings
    if not args.dry_run and active_settings is None:
        try:
            active_settings = get_settings()
        except SettingsError as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
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

        # ``active_settings`` est garanti non ``None`` ici : soit injecté par
        # l'appelant, soit chargé via ``get_settings()`` juste au-dessus (la seule
        # branche, hors dry-run, où ce module a réellement besoin d'identifiants Garmin).
        # ``raise`` plutôt qu'``assert`` (interdit par flake8-bandit/S101 hors tests) :
        # ceci trahirait un bug de ce module, jamais une entrée opérateur invalide.
        if active_settings is None:  # pragma: no cover -- inatteignable, cf. ci-dessus
            raise RuntimeError(
                "bug interne : active_settings est None hors dry-run après résolution"
            )

        # Correction C1 : 'scheduled' n'appartient pas à ``ingest_run_mode_ck``
        # (migrations/002_raw.sql) -- seul 'daily' convient sémantiquement à un run
        # planifié quotidien parmi les valeurs acceptées par la contrainte SQL.
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
                        "Le WindowFetcher injecté n'expose aucun client Garmin : "
                        "archivage des fichiers originaux sauté pour ce run."
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
