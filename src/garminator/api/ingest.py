"""Logique d'ingestion d'un envoi manuel (lot L7), indépendante de tout framework HTTP.

Ce module ne connaît pas FastAPI : il prend des octets déjà lus en mémoire et un
répertoire de destination, et retourne un résultat structuré ou lève une des erreurs de
:mod:`garminator.api.errors` / :mod:`garminator.fit.errors`. C'est ``garminator.api.upload``
(la couche HTTP) qui traduit ces erreurs en codes de statut. Cette séparation permet de
tester toute la logique métier sans dépendre de ``python-multipart`` ni ``httpx`` (voir
le rapport de fin de lot : aucun des deux n'est installé dans le venv du dépôt).

Séquence imposée par la mission du lot, dans cet ordre et jamais autrement :

1. Borne de taille sur les octets reçus (:class:`~garminator.api.errors.PayloadTooLargeError`).
2. Si le contenu est une archive ZIP, pré-inspection des métadonnées membres (noms,
   tailles annoncées) AVANT toute décompression réelle, pour écarter les chemins
   d'échappement et les bombes de décompression
   (:class:`~garminator.api.errors.HostileArchiveError`).
3. Archivage brut-d'abord via :func:`garminator.fit.archive.archive_download` : le
   fichier est écrit et vérifié sur le volume avant toute tentative de parsing — c'est
   la garantie déjà offerte par ce module (L3), reprise ici telle quelle.
4. Tentative de parsing (:func:`garminator.fit.reader.decode_fit` /
   :func:`garminator.tcx.reader.decode_tcx`) pour valider que le contenu archivé est
   effectivement exploitable, pas seulement reconnu par sa signature.

5. Si un appelant fournit une connexion (``conn``), enregistrement de la ligne
   ``raw.fit_file`` via :func:`garminator.garmin.raw_store.insert_fit_file` avec
   ``garmin_activity_id=None`` — la convention posée par
   ``migrations/012_raw_fit_file_manual_upload.sql`` pour marquer un envoi manuel.
   Fait APRÈS l'étape 3 (jamais avant) : c'est l'invariant central de tout le module
   ``raw.fit_file``, qui ne dépend pas de la provenance de la ligne.

``conn`` reste optionnel (voir :func:`ingest_upload`) : ce lot ne force pas la présence
d'une base pour valider un envoi (utile en test, ou pour un futur usage hors-ligne),
mais la couche HTTP (:mod:`garminator.api.upload`) en fournit toujours une réelle — sans
quoi le fichier archivé resterait hors de la file de transformation, exactement le
problème que la migration 012 a résolu côté schéma. L'``activity_id`` passé à
``decode_fit``/``decode_tcx`` ci-dessous est un identifiant-sentinelle purement interne
à la validation de parsing (étape 4) : il n'est jamais persisté, jamais renvoyé à
l'appelant, et ne doit jamais être confondu avec un identifiant d'activité Garmin réel
ni avec le ``garmin_activity_id=None`` inséré à l'étape 5.

Note de périmètre AB (limite assumée, documentée dans docs/RUNBOOK.md) : un envoi
manuel n'a jamais de résumé Garmin associé (pas d'``activityId``, donc aucune ligne
``raw.api_response`` correspondante) — :mod:`garminator.transform.runner` laisse donc
ces lignes en file avec un statut « reporté » (``skipped``), jamais transformées en
``core.*`` tant qu'aucun résumé ne leur est manuellement associé. Ce n'est pas un bug de
ce module : rien ici ne peut inventer un sport, un fuseau horaire ou une durée que
Garmin seul fournit.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from garminator.api.errors import HostileArchiveError, PayloadTooLargeError
from garminator.fit.archive import ArchivedFile, archive_download
from garminator.fit.errors import SourceParseError
from garminator.fit.reader import decode_fit
from garminator.garmin.raw_store import insert_fit_file
from garminator.tcx.reader import decode_tcx

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from garminator.garmin.raw_store import DBConnection

#: Taille maximale acceptée pour le corps d'un envoi (200 Mio). Un fichier FIT ou TCX
#: réel d'activité, même une ultra-distance de plusieurs heures, tient très largement
#: en dessous : cette borne protège la mémoire du service contre un envoi anormal, pas
#: contre un usage légitime.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Taille maximale acceptée pour le contenu DÉCOMPRESSÉ d'un membre d'archive ZIP.
#: Même borne que ci-dessus : un FIT/TCX légitime n'a aucune raison de dépasser 200 Mio
#: une fois décompressé.
DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES = DEFAULT_MAX_UPLOAD_BYTES

#: Ratio (taille décompressée / taille compressée) au-delà duquel un membre est traité
#: comme une bombe de décompression plutôt qu'un fichier légitime. Un TCX (XML, texte
#: répétitif) compresse bien mais reste très en dessous de ce seuil en pratique ; les
#: bombes de décompression classiques (ex. 42.zip) visent des ratios de plusieurs
#: milliers, largement au-dessus.
DEFAULT_MAX_COMPRESSION_RATIO = 200.0

#: Signatures ZIP reconnues (mêmes que ``garminator.fit.archive._ZIP_SIGNATURES`` :
#: dupliquées ici plutôt qu'importées, car ce nom est privé à ce module-là et cette
#: pré-inspection est un besoin propre à la couche HTTP, pas à l'archivage lui-même).
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")


def _looks_like_zip(data: bytes) -> bool:
    return any(data[: len(sig)] == sig for sig in _ZIP_SIGNATURES)


def _is_safe_member_name(name: str) -> bool:
    """Refuse tout nom de membre ZIP portant un chemin d'échappement.

    Défense en profondeur : ``garminator.fit.archive`` n'utilise déjà jamais ce nom pour
    écrire sur le volume (il lit le contenu par index et choisit lui-même le nom final),
    donc une évasion de chemin n'est pas exploitable dans l'état actuel du code. Mais un
    nom de membre suspect reste un signal fort d'archive malveillante ou mal formée, à
    rejeter explicitement plutôt qu'à laisser passer silencieusement.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    if ":" in name:  # chemin absolu Windows, ex. "C:\\evil"
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def check_zip_safety(
    data: bytes,
    *,
    max_member_uncompressed_bytes: int = DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    """Pré-inspecte les métadonnées d'une archive ZIP, sans rien décompresser.

    N'ouvre que :meth:`zipfile.ZipFile.infolist` (métadonnées de l'en-tête central,
    déjà lues par un ``ZipFile()`` normal) : aucun octet de contenu membre n'est
    décompressé ici, donc une bombe de décompression ne peut pas consommer de mémoire
    pendant cette vérification elle-même.

    Une archive simplement invalide (:class:`zipfile.BadZipFile`) n'est PAS une archive
    hostile : ce n'est pas ici qu'elle doit être signalée. Cette fonction se tait dans ce
    cas et laisse :func:`garminator.fit.archive.archive_download`, appelé juste après,
    lever la :class:`~garminator.fit.errors.ArchiveError` appropriée (message « archive
    ZIP invalide »).

    Raises:
        HostileArchiveError: nom de membre portant un chemin d'échappement, taille
            décompressée annoncée excessive, ou ratio de compression excessif.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not _is_safe_member_name(info.filename):
                raise HostileArchiveError(
                    f"Archive ZIP refusée : nom de membre suspect « {info.filename} » "
                    "(chemin d'échappement)."
                )
            if info.file_size > max_member_uncompressed_bytes:
                raise HostileArchiveError(
                    f"Archive ZIP refusée : membre « {info.filename} » annonce "
                    f"{info.file_size} octets décompressés, au-dessus de la borne "
                    f"({max_member_uncompressed_bytes})."
                )
            compressed = max(info.compress_size, 1)
            ratio = info.file_size / compressed
            if ratio > max_compression_ratio:
                raise HostileArchiveError(
                    f"Archive ZIP refusée : membre « {info.filename} » a un ratio de "
                    f"compression de {ratio:.0f} (borne {max_compression_ratio:.0f}), "
                    "signature caractéristique d'une bombe de décompression."
                )


#: Identifiant-sentinelle interne, jamais persisté (voir docstring du module).
_VALIDATION_ACTIVITY_ID = -1


@dataclass(frozen=True, slots=True)
class ParsedSummary:
    """Résumé du parsing d'un fichier archivé, pour la réponse HTTP.

    Ne reprend jamais les échantillons eux-mêmes (potentiellement des dizaines de
    milliers) : seulement des comptes et la présence/absence des informations clés,
    de quoi confirmer que le fichier est réellement exploitable sans gonfler la
    réponse HTTP.
    """

    n_samples: int
    n_laps: int
    n_swim_lengths: int
    has_session: bool
    start_time: datetime | None


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """Résultat complet d'un envoi traité : toujours archivé si retourné sans lever.

    ``parsed`` est ``None`` exactement quand ``parse_error`` n'est pas ``None`` — les
    deux ne sont jamais renseignés ensemble, jamais ni l'un ni l'autre absent.

    ``fit_file_id`` est ``None`` exactement quand :func:`ingest_upload` a été appelée
    sans ``conn`` (validation pure, sans base) : ce n'est jamais un signe d'échec
    d'insertion, une insertion en échec lève (``FitFileShaConflictError`` notamment)
    plutôt que de retourner silencieusement un résultat sans identifiant.
    """

    archived: ArchivedFile
    parsed: ParsedSummary | None
    parse_error: str | None
    fit_file_id: int | None = None


def _summarize(data: bytes, source_format: str) -> ParsedSummary:
    if source_format == "fit":
        parsed = decode_fit(data, activity_id=_VALIDATION_ACTIVITY_ID)
    elif source_format == "tcx":
        parsed = decode_tcx(data, activity_id=_VALIDATION_ACTIVITY_ID)
    else:  # pragma: no cover - archive_download ne retourne jamais autre chose
        raise SourceParseError(f"Format source inattendu après archivage : {source_format!r}.")
    return ParsedSummary(
        n_samples=len(parsed.samples),
        n_laps=len(parsed.laps),
        n_swim_lengths=len(parsed.swim_lengths),
        has_session=parsed.session is not None,
        start_time=parsed.start_time,
    )


def ingest_upload(
    data: bytes,
    dest_dir: Path,
    *,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    conn: DBConnection | None = None,
) -> UploadOutcome:
    """Point d'entrée unique du lot L7 : borne de taille, garde anti-ZIP-hostile,
    archivage brut-d'abord, insertion ``raw.fit_file`` (si ``conn`` fourni), puis
    validation de parsing.

    Le nom de fichier stocké est dérivé du sha256 du contenu ENVOYÉ (avant extraction
    ZIP éventuelle), jamais du nom de fichier annoncé par le client HTTP ni d'un nom de
    membre d'archive : contenu-adressé, donc insensible à toute tentative d'évasion de
    chemin côté nommage, et naturellement idempotent (un même envoi produit toujours le
    même nom final).

    ``conn`` reste optionnel : sans elle, cette fonction ne fait QUE valider et archiver
    (utile en test unitaire, aucun PostgreSQL requis) ; avec elle, la ligne ``raw.fit_file``
    est insérée avec ``garmin_activity_id=None`` (voir la docstring du module) et le
    fichier entre réellement dans la file de transformation. C'est la couche HTTP
    (:mod:`garminator.api.upload`) qui décide d'en fournir une réelle — sans quoi
    l'envoi manuel resterait archivé mais invisible pour ``garminator.transform``,
    exactement le défaut que la migration 012 corrige côté schéma.

    L'insertion a lieu APRÈS que le fichier a été écrit et fsync sur le volume par
    :func:`~garminator.fit.archive.archive_download` — jamais avant : voir l'invariant
    documenté sur ``raw.fit_file`` (``migrations/002_raw.sql``, repris par la 012). Elle
    a lieu même si le parsing de validation (étape suivante) échoue ensuite : un envoi
    archivé-mais-non-reparsable-localement doit quand même entrer en file, exactement
    comme une ligne Garmin fraîchement téléchargée — c'est ``garminator.transform`` qui
    tentera le parsing réel et posera ``parse_error`` le cas échéant, pas cette fonction.

    Raises:
        garminator.api.errors.PayloadTooLargeError: ``data`` dépasse ``max_upload_bytes``.
        garminator.api.errors.HostileArchiveError: archive ZIP au motif dangereux.
        garminator.fit.errors.ArchiveError: format non reconnu, archive vide/à plusieurs
            membres/invalide (délégué à :func:`garminator.fit.archive.archive_download`).
            Rien n'est laissé sur le volume dans ce cas.
        garminator.garmin.raw_store.FitFileShaConflictError: ``conn`` fourni et le
            sha256 du contenu envoyé est déjà associé en base à une AUTRE provenance
            (activité Garmin réelle, ou envoi manuel distinct partageant le même
            contenu). Le fichier reste archivé sur le volume (aucune perte), mais aucune
            ligne n'est insérée pour cette demande.
    """
    if len(data) > max_upload_bytes:
        raise PayloadTooLargeError(
            f"Envoi refusé : {len(data)} octets reçus, au-dessus de la borne "
            f"({max_upload_bytes})."
        )
    if _looks_like_zip(data):
        check_zip_safety(data)

    filename_stem = f"upload-{hashlib.sha256(data).hexdigest()[:16]}"
    archived = archive_download(data, dest_dir, filename_stem)

    fit_file_id: int | None = None
    if conn is not None:
        fit_file_id = insert_fit_file(
            conn,
            garmin_activity_id=None,
            storage_path=str(archived.storage_path),
            sha256=archived.sha256,
            size_bytes=archived.size_bytes,
        )

    payload = archived.storage_path.read_bytes()
    try:
        parsed = _summarize(payload, archived.source_format)
    except SourceParseError as exc:
        return UploadOutcome(
            archived=archived, parsed=None, parse_error=str(exc), fit_file_id=fit_file_id
        )
    return UploadOutcome(
        archived=archived, parsed=parsed, parse_error=None, fit_file_id=fit_file_id
    )
