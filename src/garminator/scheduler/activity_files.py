"""Archivage des fichiers « originaux » (FIT/TCX) d'activités individuelles (lot L12).

Le sondage JSON (:mod:`garminator.scheduler.service`) ne rend que le *résumé* d'une
activité : la Running Power native et le détail des longueurs de natation, les deux
signaux qui comptent réellement pour cet athlète (voir ``profil-athlete``), ne vivent que
dans le fichier ``.FIT``/``.TCX`` original. Ce module enchaîne, pour une activité donnée,
le téléchargement (:meth:`garminator.garmin.client.GarminClient.download_original`),
l'archivage sur le PVC (:func:`garminator.fit.archive.archive_download`) et
l'enregistrement de la ligne ``raw.fit_file`` (:func:`garminator.garmin.raw_store.
insert_fit_file`) -- puis une fonction de niveau supérieur qui répète cette séquence sur
la liste des activités d'une journée.

Invariant central, non négociable (repris de :mod:`garminator.fit.archive`) : **le
fichier atterrit sur le PVC AVANT toute ligne en base, jamais l'inverse**. Une ligne
``raw.fit_file`` pointant un ``storage_path`` absent est irrécupérable (le parsing L3
échouera pour toujours sur cette activité) ; un fichier orphelin sur le PVC sans ligne
associée est au pire un peu d'espace disque gaspillé, retrouvable et ré-enregistrable au
passage suivant. Le code ci-dessous respecte cet ordre par construction : ``archive_download``
est appelé, puis SEULEMENT s'il réussit, ``insert_fit_file`` l'est à son tour --
jamais l'inverse, et aucune écriture en base n'est tentée si l'archivage a levé.

Ce module n'importe ``garminconnect`` que pour ``GarminConnectAuthenticationError`` --
même choix que :mod:`garminator.garmin.client` : ``dl_fmt`` reste un paramètre opaque
(``Any``) fourni par l'appelant réel, qui possède déjà
``garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL``. Garder CE symbole-là hors de ce
module le laisse testable avec un faux client minimal, sans dépendre de la bibliothèque
réseau. ``GarminConnectAuthenticationError`` est différent : c'est un TYPE d'exception
que ce module doit reconnaître explicitement pour ne jamais la ravaler (voir la note sur
la propagation de l'authentification plus bas), pas un symbole spécifique au transport.

Idempotence, revue et corrigée (bug bloquant identifié en revue -- l'ancienne version de
ce module traitait « une ligne ``raw.fit_file`` existe pour cette activité » comme
équivalent à « le fichier est définitivement complet et correct », ce qui verrouillait à
vie une activité longue re-sondée en cours d'enregistrement Garmin, ou tout simplement
tronquée lors d'un premier passage) :

- Le DDL (``migrations/002_raw.sql``) n'impose PAS qu'un ``garmin_activity_id`` n'ait
  qu'une seule ligne ``raw.fit_file`` -- l'index ``fit_file_activity_idx ON
  raw.fit_file (garmin_activity_id, downloaded_at DESC)`` documente au contraire un
  modèle « plusieurs versions dans le temps », que ce module honore désormais.
- Pour une fenêtre CLOSE (``window_date < today``, injecté explicitement par
  l'appelant -- jamais ``date.today()`` inline, même convention que
  ``run_scheduled_poll``/``garminator.ops.backfill``), la dernière version connue est
  considérée définitive : aucun appel réseau n'est fait, ``"already_present"`` est
  retourné directement. Garmin ne documente aucune notion de fichier original modifié
  après la clôture d'une fenêtre passée.
- Pour la fenêtre EN COURS (``window_date >= today``), le fichier est retéléchargé à
  chaque passage et son sha256 comparé à la dernière version connue : contenu identique
  -> ``"already_present"`` (rien de nouveau) ; contenu différent -> nouvelle ligne
  ``raw.fit_file`` insérée, statut ``"refreshed"`` (l'activité était par exemple encore
  en cours d'enregistrement lors du passage précédent et a grossi depuis).

Sur QUELS octets porte le sha256 (défaut réel trouvé en revue, corrigé ici) : le
téléchargement « original » de Garmin est une ARCHIVE ZIP contenant le ``.fit``/``.tcx``
(voir ``GarminClient.download_original``), tandis que ``raw.fit_file.sha256`` porte le
sha du membre EXTRAIT -- c'est ``archive_bytes`` qui le calcule, après relecture du
fichier écrit. Comparer le sha de l'enveloppe ZIP à celui d'une ligne ``raw.fit_file``
ne peut donc JAMAIS coïncider : la comparaison d'idempotence échouait systématiquement,
chaque activité du jour courant était réarchivée et rapportée ``"refreshed"`` à chaque
passage, et le nom de fichier (qualifié par ce même sha) pouvait varier d'un passage à
l'autre si Garmin ne réémet pas un ZIP bit-à-bit identique -- laissant sur le PVC un
orphelin par passage, qu'aucune ligne ``raw.fit_file`` ne référence. Ce module extrait
donc le membre lui-même (:func:`garminator.fit.archive.extract_payload`) AVANT de hacher,
et ne manipule plus que ces octets-là : un seul sha, celui qui finit en base.

Ce redesign a une conséquence pour le NOM de fichier sur le PVC : deux versions d'une
même activité (une tronquée, une complète) ne doivent jamais partager le même chemin --
sinon la seconde écrase silencieusement la première pendant la fenêtre atomique
d'``archive_download`` (write temp -> fsync -> reread -> verify -> ``os.replace``), ce qui
serait inoffensif pour le contenu final mais rendrait le ``storage_path`` de l'ANCIENNE
ligne ``raw.fit_file`` incohérent avec les octets qu'il pointe désormais. Le nom de
fichier inclut donc un extrait du sha256 du contenu (``activity-{id}-{sha256[:12]}``),
pas seulement l'identifiant d'activité.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from garminconnect import GarminConnectAuthenticationError

from garminator.fit.archive import archive_download, extract_payload
from garminator.fit.errors import ArchiveError
from garminator.garmin.raw_store import DBConnection, FitFileShaConflictError, insert_fit_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from garminator.garmin.client import IngestRunTracker, OriginalActivityFile

logger = logging.getLogger(__name__)


class OriginalFileDownloader(Protocol):
    """Vue structurelle minimale sur :class:`garminator.garmin.client.GarminClient`.

    Un Protocol plutôt qu'une dépendance directe à ``GarminClient`` : ce module n'a
    besoin que de cette unique méthode, et les tests injectent un faux client qui
    l'implémente seule -- sans reconstruire un ``GarminClient`` complet (transport,
    tokenstore, rate limiter...) hors de propos ici.
    """

    def download_original(
        self,
        activity_id: str,
        *,
        dl_fmt: Any,
        tracker: IngestRunTracker | None = None,
    ) -> OriginalActivityFile: ...


@dataclass(frozen=True, slots=True)
class _LatestFitFile:
    """Dernière ligne ``raw.fit_file`` connue pour une activité (voir :func:`_latest_fit_file`)."""

    fit_file_id: int
    sha256: bytes
    storage_path: str


def _latest_fit_file(conn: DBConnection, *, garmin_activity_id: int) -> _LatestFitFile | None:
    """Dernière version archivée d'une activité, ou ``None`` si aucune n'existe encore.

    ``ORDER BY downloaded_at DESC LIMIT 1`` -- pas un simple ``LIMIT 1`` sans tri (bug
    signalé en revue : sans ``ORDER BY``, PostgreSQL ne garantit AUCUN ordre pour les
    lignes retournées, donc « la » ligne choisie pouvait varier d'un appel à l'autre y
    compris entre deux passages sur la MÊME activité multi-versions). Le tri par
    ``downloaded_at`` décroissant rend le choix déterministe : toujours la version la
    plus récemment archivée, cohérent avec l'index ``fit_file_activity_idx`` déjà posé
    sur cette paire de colonnes par ``migrations/002_raw.sql`` (donc sans coût de tri
    supplémentaire en pratique).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fit_file_id, sha256, storage_path
              FROM raw.fit_file
             WHERE garmin_activity_id = %s
             ORDER BY downloaded_at DESC
             LIMIT 1;
            """,
            (garmin_activity_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _LatestFitFile(fit_file_id=int(row[0]), sha256=bytes(row[1]), storage_path=str(row[2]))


def _dest_dir_for(archive_root: Path, window_date: date) -> Path:
    """Sous-répertoire ``archive_root/AAAA/MM`` pour la date de la fenêtre sondée.

    Un seul répertoire plat pour toutes les activités de tout l'historique finirait avec
    plusieurs dizaines de milliers d'entrées (des années d'historique, plusieurs séances
    par jour) : certains systèmes de fichiers dégradent nettement au-delà de quelques
    milliers d'entrées par répertoire, et tout outil qui liste le répertoire (sauvegarde,
    inspection manuelle) devient pénible. Le découpage par année/mois donne des
    répertoires de quelques dizaines à quelques centaines de fichiers au pire, pour un
    athlète s'entraînant plusieurs fois par jour.

    La date utilisée est celle de la FENÊTRE sondée (le jour calendaire connu de
    l'appelant, cohérent avec ``raw.poll_window``/``raw.api_response.scope_date``), pas
    une date extraite du contenu du fichier : parser le FIT pour en tirer un horodatage
    est le travail de L3, hors périmètre de ce module qui ne fait qu'archiver des octets.
    """
    return archive_root / f"{window_date:%Y}" / f"{window_date:%m}"


@dataclass(frozen=True, slots=True)
class ActivityFileOutcome:
    """Résultat du traitement d'une activité par :func:`archive_activity_file`.

    ``status`` distingue cinq cas pour que l'appelant (et l'opérateur qui lit les logs)
    n'ait jamais à déduire l'issue depuis la seule présence/absence de champs :

    - ``"archived"`` : première version de cette activité -- téléchargement + archivage +
      insertion réussis à ce passage.
    - ``"refreshed"`` : une version existait déjà, mais le contenu retéléchargé diffère
      (activité encore en cours d'enregistrement au passage précédent, par exemple) --
      nouvelle ligne ``raw.fit_file`` insérée pour cette activité.
    - ``"already_present"`` : rien de nouveau -- soit la fenêtre est close et une version
      existait déjà (aucun appel réseau fait), soit le contenu retéléchargé est identique
      à la dernière version connue.
    - ``"duplicate_content"`` : le contenu téléchargé et archivé sur le PVC a le même
      sha256 qu'une ligne ``raw.fit_file`` déjà associée à une AUTRE activité (voir
      :class:`garminator.garmin.raw_store.FitFileShaConflictError` -- scénario
      multisport/brick). Le fichier reste sur le PVC (orphelin assumé, voir
      l'invariant PVC-avant-base en tête de module) mais aucune ligne n'est créée pour
      CETTE activité : ``fit_file_id`` reste ``None``, ``error`` porte le détail.
    - ``"failed"`` : téléchargement, archivage ou insertion en échec pour un autre motif ;
      ``error`` porte le message, ``fit_file_id``/``storage_path`` restent ``None``.
    """

    activity_id: str
    status: Literal["archived", "refreshed", "already_present", "duplicate_content", "failed"]
    fit_file_id: int | None = None
    storage_path: Path | None = None
    error: str | None = None

    def as_log_line(self) -> str:
        """Rendu lisible par un opérateur, une ligne par activité traitée."""
        if self.status in ("archived", "refreshed"):
            verbe = "archivée" if self.status == "archived" else "mise à jour"
            return (
                f"activité {self.activity_id} : {verbe} -> {self.storage_path} "
                f"(fit_file_id={self.fit_file_id})"
            )
        if self.status == "already_present":
            return f"activité {self.activity_id} : déjà archivée (fit_file_id={self.fit_file_id})"
        if self.status == "duplicate_content":
            return f"activité {self.activity_id} : CONTENU DUPLIQUÉ ({self.error})"
        return f"activité {self.activity_id} : ÉCHEC ({self.error})"


def archive_activity_file(
    client: OriginalFileDownloader,
    conn: DBConnection,
    *,
    activity_id: str,
    window_date: date,
    today: date,
    archive_root: Path,
    dl_fmt: Any,
    tracker: IngestRunTracker | None = None,
) -> ActivityFileOutcome:
    """Télécharge, archive et enregistre le fichier original d'UNE activité.

    ``today`` est injecté explicitement par l'appelant (jamais ``date.today()`` inline,
    même convention que ``run_scheduled_poll``/``garminator.ops.backfill``) : c'est ce qui
    distingue une fenêtre close (idempotence stricte, aucun appel réseau) d'une fenêtre en
    cours (retéléchargement + comparaison de contenu -- voir la note d'idempotence en tête
    de module pour le raisonnement complet).

    Séquence stricte -- voir la note d'invariant en tête de module :

    1. Conversion de ``activity_id`` en entier, une seule fois, protégée : toute la suite
       réutilise ``garmin_activity_id``. Un identifiant malformé ne lève JAMAIS hors de
       cette fonction (contrat inchangé : isolation par activité), il produit un
       ``"failed"``.
    2. Recherche de la dernière version connue (:func:`_latest_fit_file`). Si la fenêtre
       est close et qu'une version existe -> ``"already_present"`` immédiat, sans réseau.
    3. ``client.download_original`` (réseau). Une ``GarminConnectAuthenticationError`` est
       repropagée SANS être convertie en ``"failed"`` (voir plus bas) ; toute autre
       exception -> ``"failed"``, rien n'est écrit nulle part. Le comptage
       ``tracker.record_error()``/``record_success()`` de cette étape est déjà fait par
       ``download_original`` lui-même -- ne pas le refaire ici évitrait un double comptage.
    4. Si une version existait déjà et que le contenu retéléchargé a le MÊME sha256 ->
       ``"already_present"`` : rien de nouveau à archiver.
    5. ``archive_download`` (PVC), avec un nom de fichier qualifié par le sha256 du
       contenu (voir la note en tête de module sur le risque d'écrasement inter-versions).
       Toute exception (:class:`ArchiveError`, ``OSError``) -> ``"failed"`` ET
       ``tracker.record_error()`` (cette étape n'est PAS déjà comptée par
       ``download_original``) ; ``archive_download`` garantit déjà qu'aucun fichier
       partiel ne reste sur le volume dans ce cas (voir sa docstring).
    6. ``insert_fit_file`` (base), UNIQUEMENT si l'étape 5 a réussi.
       :class:`FitFileShaConflictError` (scénario multisport/brick, voir
       ``raw_store.py``) -> ``"duplicate_content"`` ET ``tracker.record_error()`` ; toute
       AUTRE exception ici -> ``"failed"`` ET ``tracker.record_error()``, mais le fichier
       archivé à l'étape 5 RESTE sur le PVC (orphelin, volontairement : voir l'invariant
       en tête de module).

    Sur une authentification refusée (``GarminConnectAuthenticationError``), cette
    fonction ne l'avale JAMAIS derrière un statut ``"failed"`` : elle se propage à
    l'appelant. Un échec d'authentification réessayé activité par activité, en boucle sur
    des dizaines d'activités d'une journée, est exactement le schéma qui fait bloquer un
    compte Garmin sans 2FA (même raisonnement que ``client.py``/``retry.py``) -- il doit
    interrompre tout le traitement du jour, pas être compté comme un échec parmi d'autres.

    En dehors de ce cas, chaque activité reste isolée : cette fonction ne lève jamais pour
    un échec de téléchargement/archivage/insertion (elle les capture et les rend dans
    ``ActivityFileOutcome.error``) -- c'est :func:`archive_activities_for_day` qui appelle
    ceci en boucle sans qu'un échec individuel n'interrompe le traitement des autres
    activités du jour.
    """
    try:
        garmin_activity_id = int(activity_id)
    except (TypeError, ValueError) as exc:
        logger.warning("Identifiant d'activité invalide %r : %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    latest = _latest_fit_file(conn, garmin_activity_id=garmin_activity_id)
    if latest is not None and window_date < today:
        # Fenêtre close : Garmin ne documente aucune notion de fichier original modifié
        # après coup, épargner le réseau est sûr -- voir la note d'idempotence en tête
        # de module.
        return _already_present(activity_id, latest)

    # Découpé en deux sous-fonctions (téléchargement+comparaison, puis archivage+
    # insertion) uniquement pour rester sous la limite PLR0911 (nombre de `return` par
    # fonction) sans sacrifier la lisibilité linéaire de la séquence -- voir leurs
    # docstrings respectives pour le détail de chaque étape, déjà documenté ci-dessus.
    download_result = _download_and_check_content(
        client, activity_id=activity_id, dl_fmt=dl_fmt, tracker=tracker, latest=latest
    )
    if isinstance(download_result, ActivityFileOutcome):
        return download_result
    payload, content_sha256 = download_result

    return _archive_and_insert(
        conn,
        activity_id=activity_id,
        garmin_activity_id=garmin_activity_id,
        content=payload,
        content_sha256=content_sha256,
        window_date=window_date,
        archive_root=archive_root,
        latest=latest,
        tracker=tracker,
    )


def _already_present(activity_id: str, latest: _LatestFitFile) -> ActivityFileOutcome:
    """Raccourci commun aux deux cas « rien de nouveau » (fenêtre close, ou contenu inchangé)."""
    return ActivityFileOutcome(
        activity_id=activity_id,
        status="already_present",
        fit_file_id=latest.fit_file_id,
        storage_path=Path(latest.storage_path),
    )


def _download_and_check_content(
    client: OriginalFileDownloader,
    *,
    activity_id: str,
    dl_fmt: Any,
    tracker: IngestRunTracker | None,
    latest: _LatestFitFile | None,
) -> tuple[bytes, bytes] | ActivityFileOutcome:
    """Télécharge, désenveloppe le ZIP, puis compare au sha256 de la dernière version connue.

    Retourne soit ``(octets à archiver, leur sha256)`` pour que l'appelant poursuive vers
    l'archivage, soit directement un ``ActivityFileOutcome`` terminal (échec, ou contenu
    identique à la dernière version -- rien à archiver).

    Les octets rendus sont ceux du MEMBRE de l'archive, pas l'enveloppe ZIP servie par
    Garmin : c'est la seule façon d'obtenir un sha256 comparable à
    ``raw.fit_file.sha256`` (voir la note « Sur QUELS octets porte le sha256 » en tête de
    module -- comparer l'enveloppe ne coïncidait jamais).

    Le comptage ``tracker.record_error()``/``record_success()`` du téléchargement
    lui-même est déjà fait par ``client.download_original`` -- ne pas le refaire ici
    évite un double comptage. L'extraction, elle, n'est comptée par personne d'autre :
    son échec appelle donc bien ``record_error()`` (même traitement que l'archivage).
    """
    try:
        downloaded = client.download_original(activity_id, dl_fmt=dl_fmt, tracker=tracker)
    except GarminConnectAuthenticationError:
        # Propagation délibérée, jamais ravalée -- voir la docstring d'archive_activity_file.
        raise
    except Exception as exc:  # isolation d'échec par activité, voir docstring
        logger.warning("Téléchargement de l'activité %s en échec : %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    try:
        payload = extract_payload(downloaded.content)
    except (ArchiveError, OSError) as exc:
        if tracker is not None:
            tracker.record_error()
        logger.warning("Téléchargement de l'activité %s illisible (archive) : %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    content_sha256 = hashlib.sha256(payload).digest()
    if latest is not None and content_sha256 == latest.sha256:
        # Contenu identique à la dernière version connue : rien de nouveau à archiver,
        # même si la fenêtre était encore ouverte au moment du passage.
        return _already_present(activity_id, latest)
    return payload, content_sha256


def _archive_and_insert(
    conn: DBConnection,
    *,
    activity_id: str,
    garmin_activity_id: int,
    content: bytes,
    content_sha256: bytes,
    window_date: date,
    archive_root: Path,
    latest: _LatestFitFile | None,
    tracker: IngestRunTracker | None,
) -> ActivityFileOutcome:
    """Archive sur le PVC puis enregistre la ligne ``raw.fit_file`` -- ordre strict.

    Voir l'invariant PVC-avant-base en tête de module : ``insert_fit_file`` n'est appelé
    QUE si ``archive_download`` a réussi, jamais l'inverse.
    """
    dest_dir = _dest_dir_for(archive_root, window_date)
    # Le sha256 du contenu qualifie le nom de fichier : deux versions de la même activité
    # (tronquée puis complète) ne doivent jamais partager le même chemin sur le PVC, sous
    # peine d'écraser silencieusement la version que l'ANCIENNE ligne raw.fit_file pointe
    # encore -- voir la note en tête de module.
    # ``content`` est déjà le membre extrait (voir _download_and_check_content), donc
    # ``content_sha256`` est bien le sha qu'``archive_bytes`` recalculera après relecture :
    # le passer en ``expected_sha256`` transforme l'écriture en vérification de bout en
    # bout, et garantit que le sha du nom de fichier, celui vérifié sur disque et celui
    # inséré en base sont un seul et même nombre.
    filename_stem = f"activity-{activity_id}-{content_sha256.hex()[:12]}"
    try:
        archived = archive_download(
            content, dest_dir, filename_stem=filename_stem, expected_sha256=content_sha256
        )
    except (ArchiveError, OSError) as exc:
        if tracker is not None:
            tracker.record_error()
        logger.warning("Archivage de l'activité %s en échec : %s", activity_id, exc)
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    try:
        fit_file_id = insert_fit_file(
            conn,
            garmin_activity_id=garmin_activity_id,
            storage_path=str(archived.storage_path),
            sha256=archived.sha256,
            size_bytes=archived.size_bytes,
        )
    except FitFileShaConflictError as exc:
        if tracker is not None:
            tracker.record_error()
        # Le fichier reste volontairement sur le PVC (orphelin) : voir l'invariant en
        # tête de module. Ne PAS le supprimer ici -- un échec base ne doit jamais faire
        # disparaître un fichier déjà écrit et vérifié.
        logger.warning(
            "Insertion raw.fit_file pour l'activité %s : contenu dupliqué avec une autre "
            "activité (fichier conservé sur %s) : %s",
            activity_id,
            archived.storage_path,
            exc,
        )
        return ActivityFileOutcome(
            activity_id=activity_id, status="duplicate_content", error=str(exc)
        )
    except Exception as exc:  # isolation d'échec par activité, voir docstring
        if tracker is not None:
            tracker.record_error()
        logger.warning(
            "Insertion raw.fit_file pour l'activité %s en échec (fichier conservé sur " "%s) : %s",
            activity_id,
            archived.storage_path,
            exc,
        )
        return ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))

    return ActivityFileOutcome(
        activity_id=activity_id,
        status="refreshed" if latest is not None else "archived",
        fit_file_id=fit_file_id,
        storage_path=archived.storage_path,
    )


@dataclass(slots=True)
class DayArchiveSummary:
    """Bilan d'un appel à :func:`archive_activities_for_day`, une entrée par activité."""

    outcomes: list[ActivityFileOutcome] = field(default_factory=list)

    @property
    def archived(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "archived"]

    @property
    def refreshed(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "refreshed"]

    @property
    def already_present(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "already_present"]

    @property
    def duplicate_content(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "duplicate_content"]

    @property
    def failed(self) -> list[ActivityFileOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    def as_log_lines(self) -> list[str]:
        """Une ligne lisible par activité, dans l'ordre de traitement."""
        return [outcome.as_log_line() for outcome in self.outcomes]


def archive_activities_for_day(
    client: OriginalFileDownloader,
    conn: DBConnection,
    *,
    activity_ids: Sequence[str],
    window_date: date,
    today: date,
    archive_root: Path,
    dl_fmt: Any,
    tracker: IngestRunTracker | None = None,
) -> DayArchiveSummary:
    """Applique :func:`archive_activity_file` à chaque activité de ``activity_ids``.

    Isolation d'échec au niveau du jour : une activité qui échoue (réseau, format non
    reconnu, I/O, base) n'empêche jamais le traitement des suivantes -- exactement le
    même principe que ``run_scheduled_poll`` (lot L6) au niveau des fenêtres. Le filet
    ``except Exception`` ci-dessous est délibérément redondant avec celui déjà présent
    dans :func:`archive_activity_file` : il protège contre toute exception inattendue qui
    proviendrait malgré tout de ce niveau, pour que l'invariant « une activité en échec
    n'interrompt jamais les autres » tienne aussi dans ce cas limite.

    ``GarminConnectAuthenticationError`` fait délibérément EXCEPTION à cette isolation :
    elle traverse cette boucle sans être ravalée par le filet défensif ci-dessous (même
    raisonnement que dans :func:`archive_activity_file` -- un échec d'authentification
    réessayé activité par activité est le schéma qui fait bloquer un compte Garmin).
    """
    summary = DayArchiveSummary()
    for activity_id in activity_ids:
        try:
            outcome = archive_activity_file(
                client,
                conn,
                activity_id=activity_id,
                window_date=window_date,
                today=today,
                archive_root=archive_root,
                dl_fmt=dl_fmt,
                tracker=tracker,
            )
        except GarminConnectAuthenticationError:
            # Propagation délibérée, jamais ravalée -- voir la docstring ci-dessus.
            raise
        except Exception as exc:  # isolation d'échec, voir docstring
            logger.warning(
                "Traitement de l'activité %s : exception inattendue en dehors du chemin "
                "normal d'archive_activity_file : %s",
                activity_id,
                exc,
            )
            outcome = ActivityFileOutcome(activity_id=activity_id, status="failed", error=str(exc))
        summary.outcomes.append(outcome)
        logger.info(outcome.as_log_line())
    return summary
