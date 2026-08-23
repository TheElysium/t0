"""Backoff exponentiel avec jitter, borné, et discrimination stricte des erreurs.

Principe non négociable de ce module : un échec d'authentification ne doit **jamais**
être réessayé. C'est précisément la boucle « échec de login → nouvel essai → nouvel
essai... » qui expose le compte Garmin à un blocage. Seules les erreurs *transitoires*
(429, erreurs de connexion/5xx) entrent dans :data:`RETRYABLE_EXCEPTIONS` ; tout le reste
— dont ``GarminConnectAuthenticationError`` et ``GarminConnectInvalidFileFormatError`` —
se propage dès la première occurrence.
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

#: Types d'exception *candidats* au réessai : 429 (limitation de débit) et erreurs de
#: connexion. Le type ne suffit pas à décider — voir :func:`is_retryable`.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)

#: Marqueurs d'échec d'authentification/autorisation à chercher dans le *message* d'une
#: ``GarminConnectConnectionError``.
#:
#: Nécessaire parce que ``garminconnect`` 0.3.2 n'expose pas le code HTTP autrement :
#: ``client.py::_run_request`` — le chemin emprunté par ``connectapi()``, donc par la
#: quasi-totalité des ~130 méthodes d'endpoint — lève
#: ``GarminConnectConnectionError(f"API Error {resp.status_code} ...")`` pour **tout**
#: statut >= 400, 401 et 403 compris, sans attribut ``response`` ni sous-type dédié
#: (contrairement à ``Garmin.download()``, qui, lui, discrimine 401/429). Sans cette
#: inspection du message, un 401 sur un endpoint de données serait réessayé
#: ``max_attempts`` fois — et chaque réessai déclenche en plus un ``_refresh_session()``
#: interne vers le SSO (``_run_request`` ligne ~1154). C'est exactement le scénario qui
#: fait bloquer un compte Garmin.
NON_RETRYABLE_MESSAGE_MARKERS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
)


def is_retryable(exc: BaseException) -> bool:
    """``True`` si ``exc`` est une erreur *transitoire* qu'on peut réessayer sans risque.

    Un 429 l'est toujours (type dédié, sans ambiguïté). Une
    ``GarminConnectConnectionError`` ne l'est que si son message ne porte aucun marqueur
    d'authentification/autorisation : dans ce cas, réessayer ne peut pas réussir et ne
    fait qu'ajouter des tentatives d'authentification au compteur de Garmin.
    """
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return True
    if not isinstance(exc, RETRYABLE_EXCEPTIONS):
        return False
    message = str(exc).lower()
    return not any(marker in message for marker in NON_RETRYABLE_MESSAGE_MARKERS)


class RetryExhaustedError(RuntimeError):
    """Le nombre maximal de tentatives a été atteint sans succès."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Paramètres bornés du backoff exponentiel."""

    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_s: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts doit être >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.jitter_s < 0:
            raise ValueError("les délais doivent être positifs")


def compute_delay(policy: RetryPolicy, attempt: int, rng: random.Random) -> float:
    """Délai avant la tentative suivante : backoff exponentiel plafonné + jitter positif.

    ``attempt`` est le numéro (1-indexé) de la tentative qui vient d'échouer. Le jitter
    est strictement additif (jamais négatif) : on ne veut jamais qu'un jitter défavorable
    réduise le délai sous ce que le backoff exponentiel exige.
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
    op_name: str = "appel",
) -> T:
    """Exécute ``func`` avec réessais bornés sur les seules erreurs transitoires.

    Toute exception hors de :data:`RETRYABLE_EXCEPTIONS` (notamment
    ``GarminConnectAuthenticationError``) est propagée immédiatement, sans consommer de
    tentative supplémentaire ni dormir.

    Raises:
        RetryExhaustedError: si ``policy.max_attempts`` tentatives ont toutes échoué sur
            une erreur transitoire. L'exception d'origine est chaînée (``__cause__``).
    """
    active_policy = policy or RetryPolicy()
    active_rng = rng or random.Random()  # noqa: S311 -- jitter non cryptographique
    active_logger = logger or logging.getLogger(__name__)

    last_exc: Exception | None = None
    for attempt in range(1, active_policy.max_attempts + 1):
        try:
            return func()
        except RETRYABLE_EXCEPTIONS as exc:
            if not is_retryable(exc):
                active_logger.error(
                    "%s : échec non transitoire (%s : %s) — propagé sans réessai.",
                    op_name,
                    type(exc).__name__,
                    exc,
                )
                raise
            last_exc = exc
            if attempt == active_policy.max_attempts:
                active_logger.error(
                    "%s : échec définitif après %d tentative(s) (%s).",
                    op_name,
                    attempt,
                    type(exc).__name__,
                )
                break
            delay = compute_delay(active_policy, attempt, active_rng)
            active_logger.warning(
                "%s : tentative %d/%d échouée (%s), nouvel essai dans %.2fs.",
                op_name,
                attempt,
                active_policy.max_attempts,
                type(exc).__name__,
                delay,
            )
            sleep(delay)

    assert last_exc is not None  # noqa: S101 — atteint uniquement après le for/break ci-dessus
    raise RetryExhaustedError(
        f"{op_name} : {active_policy.max_attempts} tentative(s) épuisée(s)"
    ) from last_exc
