"""Bounded exponential backoff with jitter, and strict error discrimination.

Non-negotiable principle of this module: an authentication failure must **never** be
retried. It is precisely the "login failure -> retry -> retry..." loop that exposes the
Garmin account to a lockout. Only *transient* errors (429, connection/5xx errors) belong
to :data:`RETRYABLE_EXCEPTIONS`; everything else — including
``GarminConnectAuthenticationError`` and ``GarminConnectInvalidFileFormatError`` —
propagates on the very first occurrence.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

from garminconnect import (
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

T = TypeVar("T")

#: Exception types that are *candidates* for a retry: 429 (rate limiting) and connection
#: errors. The type alone is not enough to decide — see :func:`is_retryable`.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)

#: Authentication/authorisation failure markers to look for in the *message* of a
#: ``GarminConnectConnectionError``.
#:
#: Needed because ``garminconnect`` 0.3.2 exposes the HTTP status no other way:
#: ``client.py::_run_request`` — the path taken by ``connectapi()``, hence by nearly all
#: of the ~130 endpoint methods — raises
#: ``GarminConnectConnectionError(f"API Error {resp.status_code} ...")`` for **every**
#: status >= 400, 401 and 403 included, with no ``response`` attribute and no dedicated
#: subtype (unlike ``Garmin.download()``, which does discriminate 401/429). Without this
#: message inspection, a 401 on a data endpoint would be retried ``max_attempts`` times —
#: and each retry additionally triggers an internal ``_refresh_session()`` against SSO
#: (``_run_request`` line ~1154). That is exactly the scenario that gets a Garmin account
#: locked.
NON_RETRYABLE_MESSAGE_MARKERS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
)


def is_retryable(exc: BaseException) -> bool:
    """``True`` if ``exc`` is a *transient* error that can safely be retried.

    A 429 always is (dedicated, unambiguous type). A ``GarminConnectConnectionError`` is
    only if its message carries no authentication/authorisation marker: otherwise
    retrying cannot succeed and merely adds authentication attempts to Garmin's counter.
    """
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return True
    if not isinstance(exc, RETRYABLE_EXCEPTIONS):
        return False
    message = str(exc).lower()
    return not any(marker in message for marker in NON_RETRYABLE_MESSAGE_MARKERS)


class RetryExhaustedError(RuntimeError):
    """The maximum number of attempts was reached without success."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded parameters of the exponential backoff."""

    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_s: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.jitter_s < 0:
            raise ValueError("delays must be positive")


def compute_delay(policy: RetryPolicy, attempt: int, rng: random.Random) -> float:
    """Delay before the next attempt: capped exponential backoff plus positive jitter.

    ``attempt`` is the 1-indexed number of the attempt that just failed. The jitter is
    strictly additive (never negative): an unlucky jitter must never shorten the delay
    below what the exponential backoff requires.
    """
    exponential: float = min(policy.base_delay_s * (2 ** (attempt - 1)), policy.max_delay_s)
    jitter = float(rng.uniform(0, policy.jitter_s)) if policy.jitter_s > 0 else 0.0
    return exponential + jitter


def call_with_retry(
    func: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    logger: logging.Logger | None = None,
    op_name: str = "call",
) -> T:
    """Run ``func`` with bounded retries on transient errors only.

    Any exception outside :data:`RETRYABLE_EXCEPTIONS` (notably
    ``GarminConnectAuthenticationError``) propagates immediately, without consuming an
    extra attempt or sleeping.

    Raises:
        RetryExhaustedError: if all ``policy.max_attempts`` attempts failed on a
            transient error. The originating exception is chained (``__cause__``).
    """
    active_policy = policy or RetryPolicy()
    active_rng = rng or random.Random()  # noqa: S311 -- non-cryptographic jitter
    active_logger = logger or logging.getLogger(__name__)

    last_exc: Exception | None = None
    for attempt in range(1, active_policy.max_attempts + 1):
        try:
            return func()
        except RETRYABLE_EXCEPTIONS as exc:
            if not is_retryable(exc):
                active_logger.error(
                    "%s: non-transient failure (%s: %s) - propagated without retry.",
                    op_name,
                    type(exc).__name__,
                    exc,
                )
                raise
            last_exc = exc
            if attempt == active_policy.max_attempts:
                active_logger.error(
                    "%s: definitive failure after %d attempt(s) (%s).",
                    op_name,
                    attempt,
                    type(exc).__name__,
                )
                break
            delay = compute_delay(active_policy, attempt, active_rng)
            active_logger.warning(
                "%s: attempt %d/%d failed (%s), retrying in %.2fs.",
                op_name,
                attempt,
                active_policy.max_attempts,
                type(exc).__name__,
                delay,
            )
            sleep(delay)

    assert last_exc is not None  # noqa: S101 - only reached after the for/break above
    raise RetryExhaustedError(
        f"{op_name}: {active_policy.max_attempts} attempt(s) exhausted"
    ) from last_exc
