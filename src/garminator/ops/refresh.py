"""Rafraîchissement des vues matérialisées ``mart.*`` (lot L10).

Trois faits, vérifiés dans les migrations 007 et 008 (pas devinés), tiennent ce module :

1. **Ordre de dépendance.** Une seule vue matérialisée en lit une autre : ``mart.pmc``
   fait ``LEFT JOIN mart.daily_load`` (migration 007, CTE ``d``). Aucune autre vue de
   ``mart`` n'apparaît dans le corps SQL d'une autre (vérifié par recherche textuelle
   sur les deux migrations : la seule occurrence de ``FROM mart.`` ou ``JOIN mart.``
   est celle-là). ``mart.daily_load`` doit donc être rafraîchie avant ``mart.pmc`` ;
   les quatre autres vues (``activity_load``, ``power_curve_best``, ``hr_zone_time``,
   ``activity_wide``) ne lisent que ``core.*`` et n'ont entre elles ni avec les deux
   premières aucune contrainte d'ordre.
2. **``CONCURRENTLY`` exige un index UNIQUE.** Les six vues en portent bien un
   (``activity_load_pk``, ``daily_load_pk``, ``pmc_pk``, ``power_curve_best_pk``,
   ``hr_zone_time_pk``, ``activity_wide_pk``) -- condition documentée explicitement
   par le commentaire de ``mart.activity_load`` (migration 007) : « L'index UNIQUE
   n'est pas décoratif : il est la condition sine qua non du REFRESH MATERIALIZED VIEW
   CONCURRENTLY ».
3. **``CONCURRENTLY`` refuse une vue jamais peuplée.** ``CREATE MATERIALIZED VIEW ...``
   (sans ``WITH NO DATA``, qui n'est utilisé par aucune des deux migrations) peuple la
   vue à sa création : en fonctionnement normal, ce cas ne se présente donc qu'une
   seule fois, juste après ``python -m garminator.db migrate`` sur une base neuve, où
   PostgreSQL a néanmoins déjà peuplé les vues -- ``ispopulated`` sera ``True`` dès la
   migration appliquée. Le cas ``ispopulated = False`` ne peut donc réellement se
   produire qu'après un ``REFRESH ... WITH NO DATA`` manuel (hors de ce module) ou une
   restauration partielle depuis une sauvegarde ``pg_dump`` sans données. Ce module le
   détecte via ``pg_matviews.ispopulated`` et bascule sur un rafraîchissement NON
   concurrent pour cette vue précise plutôt que de laisser PostgreSQL renvoyer
   l'erreur brute (« materialized view "..." has not been populated »).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import psycopg

logger = logging.getLogger(__name__)

#: Vues matérialisées de ``mart``, dans l'ordre de rafraîchissement correct. Voir le
#: point 1 de la docstring du module pour la justification tirée des migrations 007/008.
MART_VIEWS: tuple[str, ...] = (
    # Rafraîchie bien qu'aucun tableau de bord ne la lise : la migration 011 en fait une
    # vue de CONTRE-EXPERTISE, dont l'écart avec mart.activity_wide révèle qu'un seuil a
    # été corrigé après l'ingestion. Cet écart n'a de sens que si elle reste à jour.
    "mart.activity_load",
    "mart.daily_load",
    "mart.pmc",
    "mart.power_curve_best",
    "mart.hr_zone_time",
    "mart.activity_wide",
)

RefreshMode = Literal["concurrently", "with_data"]


@dataclass(frozen=True, slots=True)
class RefreshStep:
    """Une étape planifiée : rafraîchir ``view`` selon ``mode``."""

    view: str
    mode: RefreshMode

    @property
    def sql(self) -> str:
        """Instruction SQL correspondante -- pas de paramètre lié : un nom de vue ne se
        paramètre pas dans ``REFRESH MATERIALIZED VIEW`` (ce n'est pas une valeur de
        colonne), et ``MART_VIEWS`` est un tuple figé du dépôt, jamais une entrée
        utilisateur."""
        if self.mode == "concurrently":
            return f"REFRESH MATERIALIZED VIEW CONCURRENTLY {self.view};"
        return f"REFRESH MATERIALIZED VIEW {self.view} WITH DATA;"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Résultat de l'exécution d'une :class:`RefreshStep`."""

    step: RefreshStep
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def refresh_plan(
    populated: Mapping[str, bool], *, views: Iterable[str] = MART_VIEWS
) -> list[RefreshStep]:
    """Détermine, pour chaque vue de ``views`` dans l'ordre donné, le mode de
    rafraîchissement à appliquer : ``with_data`` (non concurrent) si jamais peuplée
    d'après ``populated``, ``concurrently`` sinon.

    Fonction pure -- ``populated`` est fourni par l'appelant (typiquement lu depuis
    ``pg_matviews.ispopulated``), ce qui la rend testable sans PostgreSQL.

    Raises:
        KeyError: si une vue de ``views`` est absente de ``populated`` -- l'appelant
            doit avoir interrogé l'état de TOUTES les vues avant de planifier, jamais
            en supposer un par défaut (un défaut optimiste risquerait un ``CONCURRENTLY``
            refusé par PostgreSQL ; un défaut pessimiste ferait perdre le mode concurrent
            à tort, verrouillant Grafana sans raison).
    """
    steps: list[RefreshStep] = []
    for view in views:
        if view not in populated:
            raise KeyError(
                f"État de peuplement inconnu pour {view} : interrogez pg_matviews avant "
                "de planifier (voir mart_views_populated)."
            )
        mode: RefreshMode = "concurrently" if populated[view] else "with_data"
        steps.append(RefreshStep(view=view, mode=mode))
    return steps


def mart_views_populated(
    conn: psycopg.Connection[tuple[Any, ...]], *, views: Iterable[str] = MART_VIEWS
) -> dict[str, bool]:
    """Lit ``pg_matviews.ispopulated`` pour chaque vue de ``views``.

    Raises:
        RuntimeError: si une vue attendue n'apparaît pas dans ``pg_matviews`` -- schéma
            non migré, ou nom de vue erroné. Mieux vaut échouer tout de suite qu'une
            planification silencieusement incomplète.
    """
    names = list(views)
    schema_names = [name.split(".", 1)[1] for name in names]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname || '.' || matviewname, ispopulated "
            "FROM pg_matviews WHERE schemaname = 'mart' AND matviewname = ANY(%s);",
            (schema_names,),
        )
        rows = cur.fetchall()
    found = {row[0]: bool(row[1]) for row in rows}
    missing = [name for name in names if name not in found]
    if missing:
        raise RuntimeError(
            f"Vue(s) matérialisée(s) introuvable(s) dans pg_matviews : {', '.join(missing)}. "
            "Les migrations mart (007/008) ont-elles bien été appliquées ?"
        )
    return found


def refresh_all_mart_views(
    conn: psycopg.Connection[tuple[Any, ...]], *, views: Iterable[str] = MART_VIEWS
) -> list[RefreshOutcome]:
    """Rafraîchit toutes les vues de ``views``, dans l'ordre, chacune dans sa propre
    transaction autocommise.

    Une vue en échec n'empêche pas les suivantes d'être tentées -- même philosophie que
    :func:`garminator.scheduler.service.run_scheduled_poll` : un échec partiel doit
    rester visible et localisé, jamais bloquer tout le run. C'est notamment nécessaire
    ici parce que ``mart.pmc`` dépend de ``mart.daily_load`` : si le rafraîchissement de
    ``daily_load`` échoue, celui de ``pmc`` est quand même tenté (il retravaillera alors
    sur les données de ``daily_load`` d'avant cet échec, ce qui n'est pas incorrect --
    seulement moins frais -- et reste préférable à interrompre tout le run).

    ``conn`` doit être en ``autocommit=True`` : ``REFRESH MATERIALIZED VIEW CONCURRENTLY``
    ne doit pas rester ouvert dans une transaction partagée avec les autres vues, sans
    quoi un verrou pris pour l'une resterait actif pendant le rafraîchissement des
    suivantes.
    """
    populated = mart_views_populated(conn, views=views)
    plan = refresh_plan(populated, views=views)
    outcomes: list[RefreshOutcome] = []
    for step in plan:
        try:
            with conn.cursor() as cur:
                cur.execute(step.sql)
            logger.info("Vue %s rafraîchie (mode %s).", step.view, step.mode)
            outcomes.append(RefreshOutcome(step=step))
        # Volontairement large : une vue en échec ne doit jamais interrompre les suivantes.
        except Exception as exc:
            logger.warning("Rafraîchissement de %s en échec : %s", step.view, exc)
            outcomes.append(RefreshOutcome(step=step, error=str(exc)))
    return outcomes
