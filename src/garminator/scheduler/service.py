"""Boucle de sondage périodique idempotente et reprenable (lot L6).

Ce module orchestre :mod:`garminator.scheduler.planning` (quelles fenêtres sonder) et
:mod:`garminator.scheduler.store` (l'état persisté de l'avancement) autour d'un
téléchargement **injecté** (:class:`WindowFetcher`) : ``run_scheduled_poll`` ne connaît
ni ``garminconnect`` ni le réseau, et n'importe pas
:class:`garminator.garmin.client.GarminClient` -- ce qui garde ce module testable sans
réseau ET sans dépendre du câblage réel du lot L2 (hors périmètre de ce lot ; le point
d'entrée du service est responsable de fournir un ``WindowFetcher`` qui appelle
``GarminClient`` puis retourne les empreintes des payloads obtenus).

Garantie centrale : **un échec au milieu d'un run ne fait jamais perdre de fenêtre**.
Chaque fenêtre est marquée ``in_progress`` avant l'appel réseau, ``done`` seulement après
son succès complet ; une exception pendant le fetch marque la fenêtre ``failed`` et le
run continue avec les fenêtres suivantes -- ni interrompu, ni recommencé depuis le début.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from garminator.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    due_windows,
)
from garminator.scheduler.store import (
    DBConnection,
    begin_window,
    complete_window,
    fail_window,
    has_payload_been_seen,
    load_window_states,
    record_payload_seen,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

logger = logging.getLogger(__name__)

#: Nombre maximal de fenêtres traitées PAR ENDPOINT au cours d'un même run.
#:
#: Sans cette borne, le tout premier run après une installation partirait de
#: ``history_start`` (avril 2025) et enchaînerait plus de cinq cents appels réseau d'une
#: traite, par endpoint. Le limiteur de débit (:mod:`garminator.garmin.rate_limit`) les
#: espacerait sans les borner : le run durerait des heures, et une API non publique
#: sollicitée ainsi est le meilleur moyen de se faire bannir.
#:
#: La borne est sûre parce que ``due_windows`` rend les fenêtres dans l'ordre
#: CHRONOLOGIQUE CROISSANT : chaque run comble les trous les plus anciens, donc le
#: rattrapage progresse de façon monotone et aucune fenêtre n'est jamais abandonnée --
#: seulement différée. Elle est appliquée par endpoint et non globalement, sinon un
#: endpoint très en retard affamerait indéfiniment tous ceux qui le suivent dans la liste.
DEFAULT_MAX_WINDOWS_PER_ENDPOINT = 60


@dataclass(frozen=True, slots=True)
class FetchedWindow:
    """Un payload obtenu pour une fenêtre, tel que rapporté par le :class:`WindowFetcher`
    injecté. Une fenêtre peut produire plusieurs payloads (ex. plusieurs activités le
    même jour) : ``scope_key`` les distingue, ``None`` pour un endpoint agrégé au niveau
    du jour (même convention que ``raw.api_response.scope_key``)."""

    scope_key: str | None
    payload_sha256: bytes


class WindowFetcher(Protocol):
    """Callable injecté effectuant le sondage réel d'une fenêtre.

    Peut lever n'importe quelle exception (réseau, authentification, parsing) :
    :func:`run_scheduled_poll` la capture, marque la fenêtre ``failed`` et poursuit avec
    les fenêtres suivantes -- un échec sur un jour ne doit jamais empêcher le rattrapage
    des autres jours ni des autres endpoints.
    """

    def __call__(self, endpoint: str, window_date: date) -> Sequence[FetchedWindow]: ...


@dataclass(slots=True)
class WindowOutcome:
    """Résultat du traitement d'une fenêtre au sein d'un :class:`RunSummary`."""

    endpoint: str
    window_date: date
    error: str | None = None
    already_seen_count: int = 0
    new_payload_count: int = 0
    #: ``scope_key`` non nuls rapportés par le fetcher pour cette fenêtre, dans l'ordre
    #: reçu. Pour ``get_activities_by_date`` (seul endpoint à en produire, cf.
    #: ``CallShape.ACTIVITIES_BY_DATE``) ce sont les ``activityId`` du jour : c'est par
    #: eux que ``python -m garminator.scheduler`` sait quels fichiers originaux archiver,
    #: sans re-solliciter l'API pour redemander une liste qu'il vient d'obtenir.
    #: Renseigné pour TOUS les payloads, déjà vus ou non : la présence d'un fichier sur
    #: le PVC est une question distincte de la republication d'un payload JSON, et c'est
    #: ``archive_activity_file`` qui tranche, à partir de ``raw.fit_file``.
    scope_keys: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class RunSummary:
    """Bilan d'un appel à :func:`run_scheduled_poll`, une entrée par fenêtre traitée."""

    outcomes: list[WindowOutcome] = field(default_factory=list)
    #: Endpoints dont les fenêtres dues excédaient la borne de ce run : le rattrapage
    #: reprendra au run suivant. Renseigné pour que la troncature ne soit jamais
    #: silencieuse -- un rattrapage qui progresse lentement doit être visible.
    truncated_endpoints: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> list[WindowOutcome]:
        return [outcome for outcome in self.outcomes if outcome.succeeded]

    @property
    def failed(self) -> list[WindowOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.succeeded]


def run_scheduled_poll(
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
    max_windows_per_endpoint: int | None = DEFAULT_MAX_WINDOWS_PER_ENDPOINT,
) -> RunSummary:
    """Exécute un cycle de sondage sur ``endpoints``, uniquement sur les fenêtres dues.

    Pour chaque endpoint : lit l'état persisté (:func:`~garminator.scheduler.store.
    load_window_states`), calcule les fenêtres dues (:func:`~garminator.scheduler.
    planning.due_windows`), puis pour chacune, dans l'ordre chronologique croissant :

    1. la marque ``in_progress`` (persisté, avant tout appel réseau) ;
    2. appelle ``fetch_window`` ;
    3. en cas d'exception : la marque ``failed`` avec le message d'erreur, poursuit avec
       la fenêtre suivante -- rien n'est perdu, rien n'est réappliqué en double ;
    4. en cas de succès : pour chaque payload obtenu, consulte puis met à jour
       ``raw.payload_seen`` (compteur de republications, jamais de perte d'information),
       puis marque la fenêtre ``done``.

    Au plus ``max_windows_per_endpoint`` fenêtres sont traitées par endpoint et par run
    (``None`` pour ne pas borner) ; le reliquat est repris au run suivant et l'endpoint
    est signalé dans ``RunSummary.truncated_endpoints``. Voir
    :data:`DEFAULT_MAX_WINDOWS_PER_ENDPOINT` pour la raison.

    Une fenêtre déjà ``done`` en dehors de la fenêtre glissante de rafraîchissement n'est
    simplement jamais proposée par ``due_windows`` : ``fetch_window`` n'est alors pas
    appelée du tout pour elle, ce qui évite tout retéléchargement inutile après une
    reprise normale.
    """
    active_now = now if now is not None else datetime.now(UTC)
    summary = RunSummary()
    for endpoint in endpoints:
        states = load_window_states(conn, endpoint=endpoint)
        pending_dates = due_windows(
            history_start=history_start,
            today=today,
            states=states,
            now=active_now,
            refresh_lookback_days=refresh_lookback_days,
            stale_after=stale_after,
        )
        if max_windows_per_endpoint is not None and len(pending_dates) > max_windows_per_endpoint:
            logger.info(
                "Endpoint %s : %d fenêtres dues, %d traitées ce run (borne) ; le reliquat "
                "sera repris au run suivant, des plus anciennes aux plus récentes.",
                endpoint,
                len(pending_dates),
                max_windows_per_endpoint,
            )
            summary.truncated_endpoints.append(endpoint)
            pending_dates = pending_dates[:max_windows_per_endpoint]

        for window_date in pending_dates:
            outcome = WindowOutcome(endpoint=endpoint, window_date=window_date)
            begin_window(
                conn, endpoint=endpoint, window_date=window_date, ingest_run_id=ingest_run_id
            )
            try:
                fetched = fetch_window(endpoint, window_date)
            except Exception as exc:
                logger.warning(
                    "Fenêtre %s/%s en échec (%s) : marquée 'failed', rejouable au run " "suivant.",
                    endpoint,
                    window_date,
                    exc,
                )
                fail_window(conn, endpoint=endpoint, window_date=window_date, error=str(exc))
                outcome.error = str(exc)
                summary.outcomes.append(outcome)
                continue

            for item in fetched:
                if item.scope_key is not None:
                    outcome.scope_keys.append(item.scope_key)
                already_seen = has_payload_been_seen(
                    conn,
                    endpoint=endpoint,
                    scope_date=window_date,
                    scope_key=item.scope_key,
                    payload_sha256=item.payload_sha256,
                )
                if already_seen:
                    outcome.already_seen_count += 1
                else:
                    outcome.new_payload_count += 1
                record_payload_seen(
                    conn,
                    endpoint=endpoint,
                    scope_date=window_date,
                    scope_key=item.scope_key,
                    payload_sha256=item.payload_sha256,
                )
            complete_window(conn, endpoint=endpoint, window_date=window_date)
            summary.outcomes.append(outcome)
    return summary
