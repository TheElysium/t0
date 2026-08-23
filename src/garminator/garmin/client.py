"""Client Garmin garminator : session persistante, débit maîtrisé, brut d'abord.

Ce module orchestre les autres modules de :mod:`garminator.garmin` autour d'un transport
**injecté** (:class:`~garminator.garmin.transport.GarminTransport`) : ``GarminClient`` ne
construit jamais lui-même de ``garminconnect.Garmin(email, password)``. Cette
construction (avec les vrais identifiants, lus depuis ``garminator.settings``) est la
responsabilité du point d'entrée du service — hors périmètre de ce lot — précisément pour
que tout ce module reste testable sans réseau : les tests lui injectent un faux
transport.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from garminconnect import GarminConnectAuthenticationError

from garminator.garmin.archive import extract_original_filename
from garminator.garmin.rate_limit import RateLimiter
from garminator.garmin.raw_store import (
    DBConnection,
    finish_ingest_run,
    insert_api_response,
    start_ingest_run,
)
from garminator.garmin.retry import RetryPolicy, call_with_retry
from garminator.garmin.tokenstore import ensure_tokenstore, tokenstore_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import date
    from pathlib import Path

    from garminator.garmin.transport import GarminTransport

logger = logging.getLogger(__name__)


class GarminClientError(RuntimeError):
    """Erreur générique de ce module, distincte des exceptions de ``garminconnect``."""


@dataclass(frozen=True, slots=True)
class OriginalActivityFile:
    """Octets bruts et nom de fichier réel d'un téléchargement « original » d'activité.

    Le décodage (FIT ou TCX) n'est délibérément pas fait ici : c'est le rôle de L3.
    """

    activity_id: str
    filename: str
    content: bytes


@dataclass(slots=True)
class IngestRunTracker:
    """Compteurs d'une exécution d'ingestion en cours, adossée à une ligne ``raw.ingest_run``."""

    conn: DBConnection
    ingest_run_id: int
    requests: int = 0
    errors: int = 0

    def record_success(self) -> None:
        self.requests += 1

    def record_error(self) -> None:
        self.requests += 1
        self.errors += 1


@contextmanager
def ingest_run(
    conn: DBConnection,
    *,
    mode: str,
    code_version: str | None = None,
) -> Iterator[IngestRunTracker]:
    """Ouvre une ligne ``raw.ingest_run``, la clôture avec un statut cohérent en sortie.

    - Sortie normale, aucune erreur enregistrée -> ``ok``.
    - Sortie normale, au moins une erreur enregistrée via
      :meth:`IngestRunTracker.record_error` -> ``partial``.
    - Exception non rattrapée traversant le bloc -> ``failed``, avec le message de
      l'exception dans ``notes``, puis l'exception est re-levée (elle n'est jamais avalée).
    """
    run_id = start_ingest_run(conn, mode=mode, code_version=code_version)
    tracker = IngestRunTracker(conn=conn, ingest_run_id=run_id)
    try:
        yield tracker
    except Exception as exc:
        finish_ingest_run(
            conn,
            ingest_run_id=run_id,
            status="failed",
            notes={"requests": tracker.requests, "errors": tracker.errors, "error": str(exc)},
        )
        raise
    else:
        final_status = "ok" if tracker.errors == 0 else "partial"
        finish_ingest_run(
            conn,
            ingest_run_id=run_id,
            status=final_status,
            notes={"requests": tracker.requests, "errors": tracker.errors},
        )


@dataclass(slots=True)
class GarminClient:
    """Enveloppe autour d'un :class:`~garminator.garmin.transport.GarminTransport`.

    Toutes les dépendances externes (transport réseau, connexion DB, horloge, sommeil)
    sont des champs injectés avec des valeurs par défaut raisonnables pour la production,
    remplaçables en test.
    """

    transport: GarminTransport
    tokenstore_path: Path
    conn: DBConnection
    rate_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(min_interval_s=1.5))
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = field(default=time.sleep)

    def login(self) -> None:
        """Authentifie, en réutilisant le tokenstore s'il existe déjà.

        Journalise explicitement (niveau ``WARNING``) chaque occasion où le tokenstore
        était vide au moment de l'appel : c'est le signal qu'un SSO complet vient
        vraisemblablement d'avoir lieu, ce qui doit rester exceptionnel (un redémarrage de
        pod ne devrait jamais en être la cause si le volume du tokenstore est bien monté).

        N'effectue **aucun** réessai sur un échec d'authentification
        (``GarminConnectAuthenticationError``) : c'est exactement ce qui expose le compte
        Garmin à un blocage.
        """
        had_tokens = ensure_tokenstore(self.tokenstore_path)
        before = tokenstore_fingerprint(self.tokenstore_path)
        if had_tokens:
            logger.info(
                "Session Garmin restaurée depuis le tokenstore existant (%s).",
                self.tokenstore_path,
            )
        else:
            logger.warning(
                "SSO Garmin complet : le tokenstore (%s) était vide. "
                "Ceci doit rester exceptionnel — vérifier le montage du volume si ce "
                "message apparaît à chaque démarrage.",
                self.tokenstore_path,
            )

        # UNE SEULE TENTATIVE, délibérément : ``login`` n'est **jamais** passé par
        # ``call_with_retry``. Dans ``garminconnect`` 0.3.2, ``Garmin.login`` convertit
        # un 429 de SSO en ``GarminConnectTooManyRequestsError`` ("Too many login
        # attempts. Please wait a few minutes") et *tout* autre échec en
        # ``GarminConnectConnectionError(f"Login failed: {e}")`` (``__init__.py``
        # lignes ~544 et ~579) — deux types que le backoff considère transitoires.
        # Les réessayer, c'est réenchaîner des SSO complets à quelques secondes
        # d'intervalle : la boucle exacte qui fait bloquer un compte Garmin. Un échec de
        # login se règle par une nouvelle exécution planifiée, pas par un réessai serré.
        try:
            self.transport.login(tokenstore=str(self.tokenstore_path))
        except GarminConnectAuthenticationError:
            logger.error(
                "Authentification Garmin refusée : abandon immédiat, aucun réessai "
                "(un échec de login réessayé en boucle est ce qui fait bloquer un compte)."
            )
            raise
        except Exception:
            logger.exception(
                "Échec du login Garmin : abandon immédiat, aucun réessai. Même une "
                "erreur d'apparence transitoire (429 SSO, erreur de connexion) est "
                "traitée comme définitive ici — la réexécution est du ressort de "
                "l'ordonnanceur, à l'échelle de la minute, pas de la seconde."
            )
            raise
        finally:
            after = tokenstore_fingerprint(self.tokenstore_path)
            if after != before:
                logger.warning(
                    "Les jetons Garmin (%s) ont été RÉÉCRITS pendant ce login : "
                    "empreinte %s -> %s. Cela signale un SSO complet ou un "
                    "rafraîchissement de jeton, pas une simple réutilisation.",
                    self.tokenstore_path,
                    before or "<absent>",
                    after or "<absent>",
                )

    def fetch(
        self,
        endpoint: str,
        *args: Any,
        scope_date: date | None = None,
        scope_key: str | None = None,
        tracker: IngestRunTracker | None = None,
        **kwargs: Any,
    ) -> Any:
        """Appelle ``endpoint`` sur le transport, débit et backoff bornés, écrit le brut.

        ``endpoint`` est le nom de la méthode ``garminconnect`` à invoquer (ex.
        ``"get_sleep_data"``), résolu dynamiquement sur le transport injecté — ce module
        ne connaît la signature d'aucune des ~130 méthodes d'endpoint, seulement leur
        contrat commun (appelable, retourne du JSON-compatible).

        La réponse est écrite dans ``raw.api_response`` **avant** d'être retournée à
        l'appelant : c'est le principe ELT « brut d'abord ». Une erreur de transformation
        en aval se rejoue depuis ``raw`` sans re-solliciter Garmin.

        Raises:
            garminconnect.GarminConnectAuthenticationError: propagée immédiatement, sans
                réessai.
            garminator.garmin.retry.RetryExhaustedError: si les tentatives autorisées sur
                une erreur transitoire (429, connexion) sont toutes épuisées.
        """
        method = cast("Callable[..., Any]", getattr(self.transport, endpoint))

        def _call() -> Any:
            self.rate_limiter.wait()
            return method(*args, **kwargs)

        try:
            payload = call_with_retry(
                _call,
                policy=self.retry_policy,
                sleep=self.sleep,
                logger=logger,
                op_name=endpoint,
            )
        except Exception:
            if tracker is not None:
                tracker.record_error()
            raise

        response_id = insert_api_response(
            self.conn,
            endpoint=endpoint,
            payload=payload,
            scope_date=scope_date,
            scope_key=scope_key,
            ingest_run_id=tracker.ingest_run_id if tracker is not None else None,
        )
        logger.debug(
            "Réponse de %s persistée dans raw.api_response (id=%s).", endpoint, response_id
        )
        if tracker is not None:
            tracker.record_success()
        return payload

    def download_original(
        self,
        activity_id: str,
        *,
        dl_fmt: Any,
        tracker: IngestRunTracker | None = None,
    ) -> OriginalActivityFile:
        """Télécharge l'archive « originale » d'une activité (ZIP, .fit ou .tcx dedans).

        ``dl_fmt`` doit être ``garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL`` côté
        appelant réel. Ce module ne l'importe et ne le construit pas lui-même — l'appelant
        (qui possède déjà le vrai transport ``garminconnect``) le fournit, ce qui évite à
        ce module de dépendre d'un symbole spécifique à l'implémentation réseau.

        N'écrit **pas** dans ``raw.api_response`` (colonne ``payload`` typée ``jsonb``,
        inadaptée à un binaire) ni dans ``raw.fit_file`` (décision de L3, qui doit
        d'abord écrire le fichier sur le PVC avant toute ligne, règle d'atomicité §6.5).
        Ce client s'arrête à livrer les octets et le nom réel.
        """

        def _call() -> bytes:
            self.rate_limiter.wait()
            return self.transport.download_activity(activity_id, dl_fmt=dl_fmt)

        try:
            content = call_with_retry(
                _call,
                policy=self.retry_policy,
                sleep=self.sleep,
                logger=logger,
                op_name=f"download_activity[{activity_id}]",
            )
        except Exception:
            if tracker is not None:
                tracker.record_error()
            raise

        filename = extract_original_filename(content)
        if tracker is not None:
            tracker.record_success()
        return OriginalActivityFile(activity_id=activity_id, filename=filename, content=content)
