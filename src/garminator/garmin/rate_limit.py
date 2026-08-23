"""Intervalle minimal entre requêtes vers Garmin Connect.

Séparé du backoff (:mod:`garminator.garmin.retry`) : ceci borne le débit en régime
normal (aucune erreur), le backoff ne s'active qu'en réaction à une erreur transitoire.
Les deux se cumulent — un ``fetch`` attend d'abord son tour via le limiteur, puis
peut en plus reculer si Garmin répond 429.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(slots=True)
class RateLimiter:
    """Impose un intervalle minimal entre deux appels consécutifs.

    ``clock`` et ``sleep`` sont injectables pour que les tests s'exécutent instantanément
    (pas d'attente réelle) tout en exerçant la même logique qu'en production.
    """

    min_interval_s: float
    clock: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    _last_call: float | None = field(default=None, init=False, repr=False)

    def wait(self) -> None:
        """Bloque si nécessaire pour respecter ``min_interval_s`` depuis le dernier appel."""
        now = self.clock()
        if self._last_call is not None:
            remaining = self.min_interval_s - (now - self._last_call)
            if remaining > 0:
                self.sleep(remaining)
        self._last_call = self.clock()
