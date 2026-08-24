"""Minimum interval between requests to Garmin Connect.

Kept separate from the backoff (:mod:`garminator.garmin.retry`): this bounds the rate in
normal operation (no error), while the backoff only kicks in as a reaction to a transient
error. The two add up — a ``fetch`` first waits for its turn through the limiter, then
may additionally back off if Garmin answers 429.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(slots=True)
class RateLimiter:
    """Enforce a minimum interval between two consecutive calls.

    ``clock`` and ``sleep`` are injectable so that tests run instantly (no real waiting)
    while exercising the same logic as production.
    """

    min_interval_s: float
    clock: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    _last_call: float | None = field(default=None, init=False, repr=False)

    def wait(self) -> None:
        """Block if needed to honour ``min_interval_s`` since the last call."""
        now = self.clock()
        if self._last_call is not None:
            remaining = self.min_interval_s - (now - self._last_call)
            if remaining > 0:
                self.sleep(remaining)
        self._last_call = self.clock()
