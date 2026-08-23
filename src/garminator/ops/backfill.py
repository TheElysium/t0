"""Suivi et pilotage du rattrapage historique depuis ``history_start`` (lot L10).

Ce module ne réimplémente RIEN de :mod:`garminator.scheduler` : il compose
``garminator.scheduler.store.load_window_states`` (lecture de l'état persisté) et
``garminator.scheduler.planning.due_windows`` (calcul pur des fenêtres dues) --
exactement les deux fonctions que :func:`garminator.scheduler.service.run_scheduled_poll`
utilise en interne -- pour produire un compte-rendu de progression. Il n'invente aucune
notion de « fenêtre restante » différente de celle déjà définie par L6.

Ce module ne sait PAS interroger Garmin. Amorcer réellement le rattrapage (appeler
``fetch_window`` pour de vrai) suppose un adaptateur qui traduise le nom d'un des ~130
endpoints ``garminconnect`` vers l'appel réel de :class:`garminator.garmin.client.
GarminClient` -- cet adaptateur, et la liste des endpoints à sonder, n'existent nulle
part ailleurs dans le dépôt à ce jour (aucun point d'entrée de service ne câble
:mod:`garminator.scheduler` à :mod:`garminator.garmin` -- voir le rapport de fin de
lot). Deviner cette liste ou ce câblage ici serait inventer un contrat que ce lot n'a
pas les moyens de vérifier. :func:`run_backfill_cycle` ci-dessous reste donc un simple
relais typé vers :func:`garminator.scheduler.service.run_scheduled_poll`, prêt à être
appelé par ce futur point d'entrée avec un vrai ``fetch_window`` -- SANS aucune logique
propre à dupliquer plus tard. En attendant ce câblage, l'exploitation dispose de
:func:`backfill_progress`, qui répond seule à la question réellement posée en
exploitation : « combien de fenêtres restent, et depuis quand ? ».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from garminator.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    daily_range,
    due_windows,
)
from garminator.scheduler.service import (
    RunSummary,
    WindowFetcher,
    run_scheduled_poll,
)
from garminator.scheduler.store import DBConnection, load_window_states

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, timedelta

#: Début réel de l'historique disponible pour l'athlète (cf. brief L10). Distinct de
#: ``garminator.scheduler.planning`` (qui ne fixe aucune date par défaut : la date de
#: début est un paramètre d'appel, pas une constante de planification) -- c'est un fait
#: de PRODUIT (le compte Garmin de l'athlète n'a pas d'historique exploitable avant
#: cette date), pas un choix d'algorithme.
DEFAULT_HISTORY_START_ISO = "2025-04-03"


@dataclass(frozen=True, slots=True)
class EndpointBackfillProgress:
    """Progression du rattrapage pour un endpoint, sur ``[history_start, today]``."""

    endpoint: str
    total_days: int
    done: int
    due: int
    #: Sous-ensemble de ``due`` : fenêtres n'ayant jamais été tentées.
    never_attempted: int
    #: Sous-ensemble de ``due`` : fenêtres en échec (rejouées automatiquement).
    failed: int
    #: Sous-ensemble de ``due`` : fenêtres ``in_progress`` périmées (run interrompu).
    stale_in_progress: int
    #: Plus ancienne fenêtre due, ``None`` si aucune (rattrapage terminé pour cet
    #: endpoint, à la fraîcheur de la fenêtre glissante près).
    oldest_due: date | None

    @property
    def is_caught_up(self) -> bool:
        """``True`` si aucune fenêtre n'est due -- le rattrapage historique de cet
        endpoint est terminé (il resondera seulement la fenêtre glissante récente)."""
        return self.due == 0


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    """Compte-rendu de progression sur plusieurs endpoints, produit par
    :func:`backfill_progress`."""

    history_start: date
    today: date
    by_endpoint: list[EndpointBackfillProgress] = field(default_factory=list)

    @property
    def total_due(self) -> int:
        return sum(p.due for p in self.by_endpoint)

    @property
    def fully_caught_up(self) -> bool:
        return all(p.is_caught_up for p in self.by_endpoint)


def backfill_progress(
    conn: DBConnection,
    *,
    endpoints: Sequence[str],
    history_start: date,
    today: date,
    now: datetime | None = None,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
) -> BackfillProgress:
    """Calcule la progression du rattrapage historique pour chaque endpoint.

    Idempotent et sans effet de bord : lit ``raw.poll_window`` (via
    :func:`~garminator.scheduler.store.load_window_states`), ne modifie jamais rien.
    Réexécutable à volonté -- exactement ce qu'exige un point de contrôle
    d'exploitation.

    ``due`` (et ses sous-catégories) réutilise :func:`~garminator.scheduler.planning.
    due_windows` telle quelle : ce sont EXACTEMENT les fenêtres qu'un
    :func:`~garminator.scheduler.service.run_scheduled_poll` traiterait au prochain run.
    """
    active_now = now if now is not None else datetime.now(UTC)
    total_days = len(daily_range(history_start, today))
    by_endpoint: list[EndpointBackfillProgress] = []
    for endpoint in endpoints:
        states = load_window_states(conn, endpoint=endpoint)
        due = due_windows(
            history_start=history_start,
            today=today,
            states=states,
            now=active_now,
            refresh_lookback_days=refresh_lookback_days,
            stale_after=stale_after,
        )
        never_attempted = sum(1 for d in due if d not in states)
        failed = sum(1 for d in due if d in states and states[d].status == "failed")
        stale_in_progress = sum(1 for d in due if d in states and states[d].status == "in_progress")
        done = sum(1 for state in states.values() if state.status == "done")
        by_endpoint.append(
            EndpointBackfillProgress(
                endpoint=endpoint,
                total_days=total_days,
                done=done,
                due=len(due),
                never_attempted=never_attempted,
                failed=failed,
                stale_in_progress=stale_in_progress,
                oldest_due=due[0] if due else None,
            )
        )
    return BackfillProgress(history_start=history_start, today=today, by_endpoint=by_endpoint)


def run_backfill_cycle(
    conn: DBConnection,
    *,
    endpoints: Sequence[str],
    history_start: date,
    today: date,
    fetch_window: WindowFetcher,
    ingest_run_id: int | None = None,
    now: datetime | None = None,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
    max_windows_per_endpoint: int | None = None,
) -> RunSummary:
    """Relais typé vers :func:`garminator.scheduler.service.run_scheduled_poll`.

    Existe pour donner au backfill un point d'entrée nommé et documenté dans
    :mod:`garminator.ops`, sans dupliquer la moindre ligne de logique de planification
    ou de reprise -- tout est délégué. La seule différence avec un appel direct à
    ``run_scheduled_poll`` est la valeur par défaut de ``max_windows_per_endpoint`` :
    ``None`` ici (non bornée) plutôt que
    :data:`garminator.scheduler.service.DEFAULT_MAX_WINDOWS_PER_ENDPOINT` (60), car un
    rattrapage explicitement déclenché par un opérateur (ce Job, pas le sondage
    périodique) a vocation à traiter tout le reliquat en un passage plutôt qu'à
    s'étaler sur plusieurs cycles de 60 fenêtres. Un appelant qui préfère la borne
    habituelle peut la repasser explicitement.
    """
    return run_scheduled_poll(
        conn,
        endpoints=endpoints,
        history_start=history_start,
        today=today,
        fetch_window=fetch_window,
        ingest_run_id=ingest_run_id,
        now=now,
        refresh_lookback_days=refresh_lookback_days,
        stale_after=stale_after,
        max_windows_per_endpoint=max_windows_per_endpoint,
    )
