"""Lot L6 : ordonnancement du sondage périodique et idempotence.

Trois responsabilités séparées :

- :mod:`garminator.scheduler.planning` : logique pure (aucune DB, aucun réseau) décidant
  quelles fenêtres ``(endpoint, jour)`` sont dues.
- :mod:`garminator.scheduler.store` : lecture/écriture de ``raw.poll_window`` (migration
  009, avancement du sondage) et ``raw.payload_seen`` (migration 002, déduplication de
  contenu), derrière une connexion injectée.
- :mod:`garminator.scheduler.service` : orchestre les deux autour d'un téléchargement
  injecté (:class:`~garminator.scheduler.service.WindowFetcher`), sans jamais construire
  lui-même de :class:`garminator.garmin.client.GarminClient` -- ce câblage réel est la
  responsabilité du point d'entrée du service, hors périmètre de ce lot.

Garantie tenue par ce paquet : un run interrompu au milieu (panne, ``SIGTERM``) ne fait
jamais perdre de fenêtre -- elle est rejouée au run suivant, jamais silencieusement
considérée comme traitée.
"""

from __future__ import annotations

from garminator.scheduler.planning import (
    DEFAULT_REFRESH_LOOKBACK_DAYS,
    DEFAULT_STALE_IN_PROGRESS_AFTER,
    WindowState,
    WindowStatusLiteral,
    daily_range,
    due_windows,
)
from garminator.scheduler.service import (
    FetchedWindow,
    RunSummary,
    WindowFetcher,
    WindowOutcome,
    run_scheduled_poll,
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

__all__ = [
    "DEFAULT_REFRESH_LOOKBACK_DAYS",
    "DEFAULT_STALE_IN_PROGRESS_AFTER",
    "DBConnection",
    "FetchedWindow",
    "RunSummary",
    "WindowFetcher",
    "WindowOutcome",
    "WindowState",
    "WindowStatusLiteral",
    "begin_window",
    "complete_window",
    "daily_range",
    "due_windows",
    "fail_window",
    "has_payload_been_seen",
    "load_window_states",
    "record_payload_seen",
    "run_scheduled_poll",
]
