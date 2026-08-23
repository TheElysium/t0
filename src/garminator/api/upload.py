"""Endpoint HTTP d'envoi manuel d'un fichier FIT/TCX/ZIP (lot L7).

Accès prévu LOCAL uniquement pour l'instant (décision produit assumée, cf. rapport de
fin de lot) : ni Ingress, ni TLS, ni authentification ne sont mis en place ici. Ce
module ne fait AUCUNE hypothèse de format d'après l'extension du nom de fichier annoncé
par le client HTTP ni d'après une éventuelle URL : tout le sniffing est délégué à
:mod:`garminator.api.ingest` / :mod:`garminator.fit.archive`, qui ne regardent que le
contenu.

Table de correspondance erreur -> statut HTTP, volontairement explicite (pas de
« 500 » générique pour un cas prévu) :

- :class:`~garminator.api.errors.PayloadTooLargeError` -> 413 (corps de requête trop
  volumineux).
- :class:`~garminator.api.errors.HostileArchiveError` -> 422 (archive ZIP au motif
  dangereux : chemin d'échappement ou bombe de décompression).
- :class:`~garminator.fit.errors.ArchiveError` -> 422 (contenu non reconnu comme FIT ni
  TCX, archive ZIP vide/à plusieurs membres/invalide).
- Parsing échoué après archivage réussi -> 422 également, mais le corps de la réponse
  indique explicitement que le fichier brut a bien été archivé (aucune perte), pour ne
  jamais faire croire à un échec total quand seule l'ingestion l'est.
- :class:`~garminator.garmin.raw_store.FitFileShaConflictError` -> 409 (conflit) : le
  contenu envoyé est déjà associé en base à une AUTRE provenance (activité Garmin
  réelle, ou envoi manuel distinct). Le fichier reste archivé sur le volume — voir le
  détail du scénario dans la docstring de cette exception.
- Toute autre exception : laissée remonter (500 par défaut de FastAPI) — un incident
  réellement inattendu ne doit pas être maquillé en réponse « propre ».

``python-multipart`` est requis à l'exécution par ``File(...)`` de FastAPI : absent du
venv de ce dépôt au moment de l'écriture de ce lot (voir rapport de fin de lot). Ce
module reste néanmoins écrit et importable normalement — seul l'appel réel de la route
avec un vrai corps ``multipart/form-data`` en a besoin.
"""

from __future__ import annotations

# `Path` reste un import reel (pas seulement sous TYPE_CHECKING) : FastAPI resout les
# annotations `Annotated[Path, Depends(...)]` a l'execution via get_type_hints(), qui a
# besoin de `Path` dans l'espace de noms du module.
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, cast

import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from garminator.api.errors import HostileArchiveError, PayloadTooLargeError, UploadError
from garminator.api.ingest import DEFAULT_MAX_UPLOAD_BYTES, ingest_upload
from garminator.fit.errors import ArchiveError
from garminator.garmin.raw_store import FitFileShaConflictError
from garminator.settings import get_settings

if TYPE_CHECKING:
    # Importé pour le seul `cast` ci-dessous : `DBConnection` est un protocole, il n'a
    # aucune existence à l'exécution. Même convention que garminator.transform.__main__.
    from collections.abc import Iterator

    from garminator.garmin.raw_store import DBConnection

router = APIRouter(prefix="/uploads", tags=["uploads"])

#: Taille d'un morceau de lecture pendant le streaming du corps de requête. Choisie
#: petite (1 Mio) pour que la borne de taille (voir :mod:`garminator.api.ingest`) coupe
#: rapidement un envoi excessif, sans avoir à matérialiser tout le corps en mémoire
#: avant de s'en apercevoir.
_READ_CHUNK_BYTES = 1024 * 1024


def get_upload_dest_dir() -> Path:
    """Dépendance FastAPI par défaut : répertoire d'archivage lu depuis les settings.

    Surchargeable en test via ``app.dependency_overrides`` (voir ``tests/api/``), pour
    ne jamais dépendre de variables d'environnement Garmin/PostgreSQL dans un test qui
    ne teste que l'archivage d'un envoi.
    """
    return get_settings().fit_archive_path


def get_upload_db_conn() -> Iterator[DBConnection]:
    """Dépendance FastAPI par défaut : connexion PostgreSQL réelle, une par requête.

    Générateur plutôt que simple retour, pour que FastAPI ferme la connexion après la
    requête quel que soit son issue (succès, 4xx, exception non prévue) — un
    ``finally`` explicite plutôt qu'espérer que le garbage collector s'en charge, ce qui
    laisserait une connexion PostgreSQL ouverte par envoi sous charge.

    Surchargeable en test via ``app.dependency_overrides`` (voir ``tests/api/``), pour
    injecter une doublure en mémoire sans jamais se connecter à un PostgreSQL réel —
    nécessaire ici puisqu'aucune instance PostgreSQL n'existe dans cet environnement de
    développement (voir le rapport de fin de lot).

    ``cast`` plutôt qu'un ``psycopg.Connection`` renvoyé tel quel : même convention que
    ``garminator.transform.__main__`` / ``garminator.ops.__main__`` — mypy refuse de
    rapprocher la méthode ``cursor`` SURCHARGÉE de psycopg d'un protocole structurel qui
    ne l'est pas.
    """
    conn = psycopg.connect(get_settings().postgres_dsn.get_secret_value())
    try:
        yield cast("DBConnection", conn)
    finally:
        conn.close()


class ParsedSummaryOut(BaseModel):
    """Reflet HTTP de :class:`garminator.api.ingest.ParsedSummary`."""

    n_samples: int
    n_laps: int
    n_swim_lengths: int
    has_session: bool
    start_time: str | None


class UploadResponse(BaseModel):
    """Corps de réponse d'un envoi traité, succès d'ingestion ou non.

    ``status`` vaut ``"ok"`` quand l'archivage ET le parsing ont réussi, ``"archived_but_unparsed"``
    quand seul l'archivage a réussi (le fichier brut n'est PAS perdu, mais l'ingestion ne
    peut pas continuer sans intervention). Jamais ``"ok"`` sans que ``parsed`` soit
    renseigné : ce champ existe précisément pour qu'un appelant ne puisse pas confondre
    les deux cas en ne lisant que le code HTTP.
    """

    status: str
    source_format: str
    sha256: str
    size_bytes: int
    parsed: ParsedSummaryOut | None
    parse_error: str | None
    fit_file_id: int


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Lit ``file`` par morceaux, coupant dès que ``max_bytes`` est dépassé.

    Évite de matérialiser en mémoire un corps de requête déjà su excessif : contrairement
    à un simple ``await file.read()`` suivi d'une vérification de taille après coup, la
    lecture s'arrête au premier morceau qui fait franchir la borne.

    Raises:
        garminator.api.errors.PayloadTooLargeError: la taille cumulée dépasse ``max_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"Envoi refusé : au moins {total} octets reçus, au-dessus de la borne "
                f"({max_bytes})."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=UploadResponse, status_code=201)
async def upload_activity_file(
    file: Annotated[UploadFile, File(description="Fichier .fit, .tcx, ou archive .zip.")],
    dest_dir: Annotated[Path, Depends(get_upload_dest_dir)],
    conn: Annotated[DBConnection, Depends(get_upload_db_conn)],
) -> UploadResponse:
    """Reçoit un fichier FIT/TCX/ZIP, l'archive brut-d'abord, insère sa ligne
    ``raw.fit_file`` (``garmin_activity_id=None``), puis tente de le parser."""
    data = await _read_capped(file, DEFAULT_MAX_UPLOAD_BYTES)

    try:
        outcome = ingest_upload(
            data, dest_dir, max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES, conn=conn
        )
    except PayloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except HostileArchiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArchiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FitFileShaConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UploadError as exc:  # pragma: no cover - garde-fou si une sous-classe est ajoutée
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if outcome.fit_file_id is None:  # pragma: no cover - garde-fou, `conn` est fourni ci-dessus
        raise RuntimeError(
            "ingest_upload a retourné fit_file_id=None alors qu'une connexion a été "
            "fournie : incohérence interne entre cette route et garminator.api.ingest."
        )

    parsed_out = (
        ParsedSummaryOut(
            n_samples=outcome.parsed.n_samples,
            n_laps=outcome.parsed.n_laps,
            n_swim_lengths=outcome.parsed.n_swim_lengths,
            has_session=outcome.parsed.has_session,
            start_time=(
                outcome.parsed.start_time.isoformat() if outcome.parsed.start_time else None
            ),
        )
        if outcome.parsed is not None
        else None
    )
    response = UploadResponse(
        status="ok" if outcome.parse_error is None else "archived_but_unparsed",
        source_format=outcome.archived.source_format,
        sha256=outcome.archived.sha256.hex(),
        size_bytes=outcome.archived.size_bytes,
        parsed=parsed_out,
        parse_error=outcome.parse_error,
        fit_file_id=outcome.fit_file_id,
    )
    if outcome.parse_error is not None:
        # Le fichier brut EST archivé (aucune perte) mais l'ingestion a échoué : ce
        # n'est pas un succès, donc pas de 201 silencieux. Le corps ci-dessus reste
        # transmis via `detail` pour que l'appelant sache que l'original est en sûreté.
        raise HTTPException(status_code=422, detail=response.model_dump())
    return response
