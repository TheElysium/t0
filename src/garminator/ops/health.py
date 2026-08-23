"""État de santé exploitable du pipeline (lot L10).

Chaque fonction de ce module répond à une question posée en exploitation réelle, et
SEULEMENT depuis ce qui existe réellement en base :

- « Quand a-t-on ingéré pour la dernière fois, par endpoint ? » -- lu depuis
  ``raw.poll_window.completed_at`` (migration 009), qui n'est renseigné que sur un
  succès complet (cf. ``garminator.scheduler.store.complete_window``).
- « Quelles fenêtres sont en échec ? » -- ``raw.poll_window`` où ``status = 'failed'``.
- « Quelles fenêtres sont bloquées ``in_progress`` ? » -- même table, ``status =
  'in_progress'`` et ``last_attempted_at`` plus vieux que le seuil de péremption déjà
  défini par L6 (:data:`garminator.scheduler.planning.DEFAULT_STALE_IN_PROGRESS_AFTER`),
  réutilisé ici plutôt que redéfini.
- « Les vues ``mart`` sont-elles à jour ? » -- comparaison entre
  ``MAX(local_date)`` de chaque vue et ``MAX(local_date)`` de ``core.activity`` (la
  donnée source la plus fraîche déjà transformée), plus ``pg_matviews.ispopulated``.
- « Où sont les trous du calendrier ? » -- déléguée à
  :func:`garminator.ops.backfill.backfill_progress`, qui EST déjà la réponse à cette
  question (une fenêtre « due » est un trou : jamais tentée, en échec, ou bloquée).

Aucune table n'est inventée : tout est lu depuis ``raw.poll_window``, ``core.activity``
et le catalogue système ``pg_matviews``, qui existent tous avant ce lot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from garminator.ops.refresh import MART_VIEWS
from garminator.scheduler.planning import DEFAULT_STALE_IN_PROGRESS_AFTER

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import timedelta

    import psycopg


@dataclass(frozen=True, slots=True)
class LastSuccess:
    """Dernière fenêtre menée à bien pour un endpoint (``None`` si jamais)."""

    endpoint: str
    last_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class FailedWindow:
    """Une fenêtre ``failed``, telle que persistée dans ``raw.poll_window``."""

    endpoint: str
    window_date: date
    attempts: int
    last_attempted_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StuckWindow:
    """Une fenêtre ``in_progress`` plus vieille que le seuil de péremption."""

    endpoint: str
    window_date: date
    attempts: int
    last_attempted_at: datetime | None


@dataclass(frozen=True, slots=True)
class MartViewFreshness:
    """Fraîcheur d'une vue ``mart`` par rapport à ``core.activity``."""

    view: str
    populated: bool
    #: ``None`` si la vue est vide (peuplée mais sans ligne, ou non peuplée).
    latest_local_date: date | None
    #: ``None`` si ``core.activity`` est vide.
    latest_core_local_date: date | None

    @property
    def lag_days(self) -> int | None:
        """Écart en jours entre la donnée source la plus fraîche et la vue. ``None``
        si l'une des deux dates manque (rien à comparer)."""
        if self.latest_local_date is None or self.latest_core_local_date is None:
            return None
        return (self.latest_core_local_date - self.latest_local_date).days


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Bilan de santé agrégé, produit par :func:`build_health_report`."""

    generated_at: datetime
    last_success: list[LastSuccess]
    failed_windows: list[FailedWindow]
    stuck_windows: list[StuckWindow]
    mart_freshness: list[MartViewFreshness]


def last_success_by_endpoint(conn: psycopg.Connection[tuple[Any, ...]]) -> list[LastSuccess]:
    """``MAX(completed_at)`` par endpoint parmi les fenêtres ``done``.

    Un endpoint qui n'apparaît dans ``raw.poll_window`` qu'avec des fenêtres
    ``pending``/``failed``/``in_progress`` (jamais ``done``) apparaît quand même dans
    le résultat, avec ``last_completed_at = None`` -- c'est précisément le cas qu'un
    opérateur doit voir (endpoint qui n'a jamais réussi une seule fois), pas un cas à
    faire disparaître par un ``WHERE completed_at IS NOT NULL``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT endpoint, MAX(completed_at) FROM raw.poll_window "
            "GROUP BY endpoint ORDER BY endpoint;"
        )
        rows = cur.fetchall()
    return [LastSuccess(endpoint=row[0], last_completed_at=row[1]) for row in rows]


def failed_windows(conn: psycopg.Connection[tuple[Any, ...]]) -> list[FailedWindow]:
    """Toutes les fenêtres actuellement ``failed``, les plus anciennes d'abord.

    Note d'exploitation : une fenêtre ``failed`` est rejouée automatiquement au
    prochain sondage (:func:`garminator.scheduler.planning.due_windows` la considère
    due) -- cette liste est un signal de surveillance, pas une file à traiter à la
    main.
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
    """Fenêtres ``in_progress`` plus vieilles que ``stale_after``.

    Même seuil que :func:`garminator.scheduler.planning.due_windows` par défaut : une
    fenêtre listée ici sera de toute façon rejouée au prochain sondage. Cette fonction
    sert à la SURVEILLER (par exemple alerter si le nombre reste élevé d'un run à
    l'autre, signe d'un sondage qui ne tourne plus du tout), pas à agir dessus --
    aucune action manuelle n'est nécessaire : voir ``docs/RUNBOOK.md``.
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
    """Fraîcheur de chaque vue de ``views`` par rapport à ``core.activity``.

    ``latest_core_local_date`` est calculé UNE fois (même valeur pour toutes les
    vues) : c'est la donnée source la plus fraîche déjà transformée par le lot L1-L5,
    donc le plafond de fraîcheur qu'un ``REFRESH`` peut atteindre.
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
                cur.execute(f"SELECT MAX(local_date) FROM {view};")  # noqa: S608 -- `view` vient de MART_VIEWS, jamais d'une entrée utilisateur
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
    """Assemble le bilan complet en un seul appel -- pratique pour la CLI et pour un
    éventuel export vers un exportateur de métriques plus tard (hors périmètre ici)."""
    active_now = now if now is not None else datetime.now(UTC)
    return HealthReport(
        generated_at=active_now,
        last_success=last_success_by_endpoint(conn),
        failed_windows=failed_windows(conn),
        stuck_windows=stuck_in_progress_windows(conn, stale_after=stale_after, now=active_now),
        mart_freshness=mart_views_freshness(conn),
    )


#: Nombre maximum de fenêtres listées nommément dans le rapport texte. Au-delà, seul le
#: total est affiché : un rapport de santé qui déroule des centaines de lignes en console
#: (ou dans les logs d'un CronJob) cesse d'être lu. Le détail complet reste interrogeable
#: en SQL sur ``raw.poll_window``.
_MAX_LIGNES_LISTEES = 20


def format_health_report(report: HealthReport) -> str:
    """Rendu texte simple, dans le même esprit que
    :func:`garminator.db.runner.format_status`."""
    lines = [
        f"Bilan de santé au {report.generated_at.isoformat()}",
        "",
        "Dernier succès par endpoint :",
    ]
    if report.last_success:
        for entry in report.last_success:
            completed = entry.last_completed_at.isoformat() if entry.last_completed_at else "JAMAIS"
            lines.append(f"  {entry.endpoint} : {completed}")
    else:
        lines.append("  (aucun endpoint connu dans raw.poll_window)")

    lines.append("")
    lines.append(f"Fenêtres en échec ({len(report.failed_windows)}) :")
    for fw in report.failed_windows[:_MAX_LIGNES_LISTEES]:
        lines.append(
            f"  {fw.endpoint} / {fw.window_date} (tentatives={fw.attempts}) : {fw.last_error}"
        )
    if len(report.failed_windows) > _MAX_LIGNES_LISTEES:
        lines.append(f"  ... et {len(report.failed_windows) - _MAX_LIGNES_LISTEES} de plus.")

    lines.append("")
    lines.append(f"Fenêtres bloquées in_progress ({len(report.stuck_windows)}) :")
    for sw in report.stuck_windows[:_MAX_LIGNES_LISTEES]:
        lines.append(
            f"  {sw.endpoint} / {sw.window_date} (dernière tentative {sw.last_attempted_at})"
        )
    if len(report.stuck_windows) > _MAX_LIGNES_LISTEES:
        lines.append(f"  ... et {len(report.stuck_windows) - _MAX_LIGNES_LISTEES} de plus.")

    lines.append("")
    lines.append("Fraîcheur des vues mart :")
    for mv in report.mart_freshness:
        if not mv.populated:
            lines.append(f"  {mv.view} : JAMAIS PEUPLÉE")
        else:
            lag = mv.lag_days
            lag_txt = f"{lag} j de retard" if lag else "à jour"
            lines.append(f"  {mv.view} : {mv.latest_local_date} ({lag_txt})")

    return "\n".join(lines)
