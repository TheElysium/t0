"""Planification pure des fenêtres de sondage (lot L6).

Aucune fonction de ce module n'accède à la base ni au réseau : elles reçoivent l'état
connu des fenêtres (tel que lu depuis ``raw.poll_window`` par
:mod:`garminator.scheduler.store`) en argument et renvoient les dates à (re)sonder.
C'est ce qui les rend testables sans PostgreSQL.

Principe d'ordonnancement (cf. ``COMMENT ON TABLE raw.poll_window``, migration 009) :
une fenêtre est due si

1. elle n'a jamais été tentée (rattrapage d'historique / nouvelle installation) ;
2. sa dernière tentative a échoué (``failed``) -- on ne l'abandonne jamais silencieusement ;
3. elle est bloquée ``in_progress`` depuis plus longtemps que ``stale_after`` -- résidu
   d'un run interrompu (panne, ``OOMKilled``, ``SIGTERM`` de Kubernetes) : sans ce cas,
   une panne au milieu d'un run laisserait la fenêtre indéfiniment coincée, jamais rejouée,
   et l'activité correspondante serait perdue ;
4. elle est ``done`` mais tombe dans la fenêtre glissante récente (``refresh_lookback_days``)
   -- Garmin republie parfois une journée après coup (activité renommée, sport corrigé,
   arrivée en retard), et l'idempotence ne doit pas empêcher cette réingestion légitime.

Une fenêtre ``done`` en dehors de la fenêtre glissante n'est JAMAIS due : c'est ce qui
évite de retélécharger tout l'historique après une simple reprise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Valeurs autorisées par ``poll_window_status_ck`` (migration 009).
WindowStatusLiteral = Literal["pending", "in_progress", "done", "failed"]

#: Fenêtre glissante par défaut resondée systématiquement même si déjà ``done`` :
#: rattrape les republications tardives de Garmin sans repasser par tout l'historique.
DEFAULT_REFRESH_LOOKBACK_DAYS = 3

#: Durée par défaut au-delà de laquelle une fenêtre ``in_progress`` est considérée comme
#: le résidu d'un run interrompu plutôt que d'un sondage réellement concurrent.
DEFAULT_STALE_IN_PROGRESS_AFTER = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class WindowState:
    """État connu d'une fenêtre (endpoint, jour), tel que lu dans ``raw.poll_window``."""

    status: WindowStatusLiteral
    attempts: int
    last_attempted_at: datetime | None
    last_error: str | None


def daily_range(start: date, end: date) -> list[date]:
    """Toutes les dates de ``start`` à ``end`` incluses, ordre croissant.

    Raises:
        ValueError: si ``start`` est postérieur à ``end``.
    """
    if start > end:
        raise ValueError(f"start ({start}) doit être <= end ({end})")
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def due_windows(
    *,
    history_start: date,
    today: date,
    states: Mapping[date, WindowState],
    now: datetime,
    refresh_lookback_days: int = DEFAULT_REFRESH_LOOKBACK_DAYS,
    stale_after: timedelta = DEFAULT_STALE_IN_PROGRESS_AFTER,
) -> list[date]:
    """Détermine les fenêtres à (re)sonder, dans l'ordre chronologique croissant.

    L'ordre croissant est délibéré : un rattrapage d'historique après une longue panne
    comble d'abord les plus vieux trous plutôt que de sonder le jour courant en boucle
    tant que le passé n'est pas rattrapé.

    Raises:
        ValueError: si ``refresh_lookback_days`` est négatif, ou si ``history_start`` est
            postérieur à ``today``.
    """
    if refresh_lookback_days < 0:
        raise ValueError("refresh_lookback_days doit être >= 0")
    refresh_floor = today - timedelta(days=refresh_lookback_days)

    due: list[date] = []
    for day in daily_range(history_start, today):
        state = states.get(day)
        if state is None or state.status in ("pending", "failed"):
            due.append(day)
        elif state.status == "in_progress":
            attempted_at = state.last_attempted_at
            if attempted_at is None or (now - attempted_at) > stale_after:
                due.append(day)
        elif state.status == "done" and day >= refresh_floor:
            due.append(day)
    return due
