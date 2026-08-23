"""Orchestration du passage ``raw.fit_file`` -> ``core.*`` : la file d'attente de parsing.

Ce module est le chaînon qui manquait pour que le projet produise quoi que ce soit de
visible. Jusqu'ici la chaîne s'arrêtait à ``raw`` : les fichiers étaient téléchargés,
archivés sur le PVC et inscrits dans ``raw.fit_file``, mais RIEN n'écrivait dans
``core.*``. Conséquence exacte, qui ne saute pas aux yeux : les vues ``mart.*`` étaient
rafraîchies avec succès à chaque passage d'``ops refresh``, sans la moindre erreur, et
restaient vides -- les tableaux de bord Grafana n'affichaient rien sans qu'aucun voyant
ne soit au rouge.

La file d'attente n'est pas inventée ici : ``raw.fit_file`` la porte depuis la
migration 002, avec ``parsed_at`` / ``parser_version`` / ``parse_error`` et l'index
partiel ``fit_file_unparsed_idx ON raw.fit_file (downloaded_at) WHERE parsed_at IS
NULL``. Ce module se contente de la consommer dans l'ordre que cet index sert.

ORDRE D'ÉCRITURE, ET POURQUOI CELUI-LÀ. Le bundle est écrit et VALIDÉ d'abord, puis
``parsed_at`` est posé dans une seconde transaction. Ce n'est pas un oubli : c'est le
même raisonnement que la règle d'atomicité « fichier sur le PVC AVANT sa ligne en
base » du commentaire de ``raw.fit_file``, appliquée un cran plus loin. Une panne
entre les deux laisse une activité écrite dans ``core`` et un fichier toujours marqué
non parsé -- le passage suivant le reprend, et comme
:func:`~garminator.transform.store.store_activity_bundle` est idempotente (remplacement
complet par activité), l'état final est identique. L'ordre inverse produirait un
fichier marqué parsé dont l'activité n'a jamais été écrite : une séance perdue en
silence, que plus aucun passage ne reprendrait jamais.

POURQUOI UN ÉCHEC N'EST PAS REJOUÉ AUTOMATIQUEMENT. La sélection par défaut ajoute
``AND parse_error IS NULL`` à la condition ``parsed_at IS NULL``. Sans cela, un fichier
définitivement indigeste -- un ``typeKey`` Garmin absent de la table de correspondance,
une archive tronquée -- serait retenté à chaque passage nocturne, pour échouer à
l'identique, indéfiniment. ``retry_failed=True`` (option ``--retry-failed``) rejoue
explicitement ces lignes, ce qui est exactement ce qu'on veut APRÈS avoir corrigé la
cause : c'est une décision d'opérateur, pas un comportement par défaut.

RÉSUMÉ ABSENT ≠ ÉCHEC. ``build_core_rows`` a besoin d'un ``ActivityContext`` que seul le
résumé JSON de Garmin porte (sport, fuseau, durée). Si ce résumé n'est pas encore dans
``raw.api_response``, le fichier est IGNORÉ sans être marqué en erreur : c'est un état
transitoire normal (le fichier .FIT et la fenêtre ``get_activities_by_date`` du jour ne
sont pas nécessairement ingérés par le même passage), et le marquer en erreur
l'exclurait de la sélection par défaut jusqu'à une intervention manuelle, pour une
cause qui se résout d'elle-même au prochain sondage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from garminator.fit.reader import decode_fit_file
from garminator.tcx.reader import decode_tcx_file
from garminator.transform.activity import build_core_rows
from garminator.transform.store import store_activity_bundle
from garminator.transform.summary import build_activity_context, load_thresholds, select_thresholds

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from garminator.load.thresholds import Threshold
    from garminator.load.trimp import Sex

logger = logging.getLogger(__name__)

#: Inscrite dans ``raw.fit_file.parser_version`` à chaque succès. Sa raison d'être est
#: écrite dans le commentaire de la colonne : « permet de rejouer sélectivement les
#: fichiers parsés par une version buguée ». Elle n'a donc d'utilité que si elle CHANGE
#: quand le comportement du décodage ou de la transformation change -- à incrémenter
#: délibérément, jamais dérivée automatiquement d'une version de paquet.
PARSER_VERSION = "garminator-transform/1"

#: Plafond par passage. Un CronJob nocturne n'a aucune raison de traiter les 1 090
#: fichiers de l'historique d'un coup au premier démarrage : mieux vaut plusieurs
#: passages courts, dont chacun laisse la base dans un état cohérent, qu'un unique
#: passage de plusieurs heures qu'une éviction de pod ramènerait à zéro.
DEFAULT_BATCH_LIMIT = 200

_PENDING_QUERY_NOT_FAILED = (
    "SELECT fit_file_id, garmin_activity_id, storage_path "
    "FROM raw.fit_file "
    "WHERE parsed_at IS NULL AND parse_error IS NULL "
    "ORDER BY downloaded_at "
    "LIMIT %s"
)

_PENDING_QUERY_ALL = (
    "SELECT fit_file_id, garmin_activity_id, storage_path "
    "FROM raw.fit_file "
    "WHERE parsed_at IS NULL "
    "ORDER BY downloaded_at "
    "LIMIT %s"
)


class _Cursor(Protocol):
    """Surface minimale exigée d'un curseur par CE module.

    ``executemany`` y figure alors qu'aucune fonction d'ici ne l'appelle : c'est
    :func:`~garminator.transform.store.store_activity_bundle`, à qui la connexion est
    transmise telle quelle, qui en a besoin. Sans lui, le protocole d'ici ne serait pas
    un sous-type de celui de ``store`` et le passage de relais ne tiendrait pas au
    typage -- alors qu'il fonctionnerait à l'exécution. Mieux vaut l'écrire que le
    contourner par un ``cast``.
    """

    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def fetchall(self) -> list[Any]: ...
    def fetchone(self) -> Any: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Vue structurelle sur une connexion psycopg 3, même motif que
    ``garminator.garmin.raw_store``, élargie à ``rollback`` : ce module en a un besoin
    que ses voisins n'ont pas, expliqué dans :func:`_mark_failed`."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingFile:
    """Une ligne de ``raw.fit_file`` restant à transformer."""

    fit_file_id: int
    #: NULL pour un envoi manuel : la migration 012 a rendu la colonne nullable, NULL
    #: étant le seul marqueur de provenance (l'API Garmin ne produit jamais de ligne
    #: sans activityId).
    garmin_activity_id: int | None
    storage_path: str


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """Sort d'un fichier. ``status`` vaut ``stored`` | ``skipped`` | ``failed``."""

    fit_file_id: int
    garmin_activity_id: int | None
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TransformReport:
    """Bilan d'un passage, destiné à la sortie standard du CronJob."""

    outcomes: tuple[FileOutcome, ...]
    #: Fichiers portant un ``parse_error`` dans TOUTE la table, pas seulement dans ce
    #: passage. Un fichier en echec sort de la selection par defaut : sans ce total, le
    #: passage suivant le trouve absent de la file, sort en 0, et l'activite manque
    #: definitivement dans ``core`` sans qu'aucun voyant ne s'allume.
    failed_backlog: int = 0

    @property
    def stored(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "stored")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


def count_failed_files(conn: DBConnection) -> int:
    """Nombre de fichiers portant un ``parse_error``, tous passages confondus.

    Un fichier marque en echec quitte la selection par defaut : il n'est plus jamais
    retente sans ``--retry-failed``. Le compter a chaque passage est ce qui empeche un
    echec unique de devenir un trou permanent dans ``core`` que plus rien ne signale.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.fit_file WHERE parse_error IS NOT NULL")
        row = cur.fetchone()
    return 0 if row is None else int(row[0])


def select_pending_files(
    conn: DBConnection, *, limit: int = DEFAULT_BATCH_LIMIT, retry_failed: bool = False
) -> tuple[PendingFile, ...]:
    """Fichiers restant à transformer, du plus ancien téléchargement au plus récent.

    ``ORDER BY downloaded_at`` n'est pas cosmétique : c'est la colonne de l'index
    partiel ``fit_file_unparsed_idx``, donc le seul ordre que PostgreSQL puisse servir
    sans tri. Traiter du plus ancien au plus récent a par ailleurs une conséquence
    métier : le PMC se construit chronologiquement, et un passage interrompu à
    mi-parcours laisse alors un historique continu jusqu'à une date, plutôt qu'un
    gruyère.
    """
    # Deux requêtes ÉCRITES EN ENTIER plutôt qu'un fragment interpolé : rien ici ne vient
    # de l'extérieur, mais une requête assemblée par f-string devient tôt ou tard le
    # gabarit qu'on recopie ailleurs avec une valeur qui, elle, en vient.
    query = _PENDING_QUERY_ALL if retry_failed else _PENDING_QUERY_NOT_FAILED
    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        rows = cur.fetchall()
    return tuple(
        PendingFile(
            fit_file_id=int(r[0]),
            # `int(None)` lèverait ici, HORS de l'isolation par fichier : un seul envoi
            # manuel ferait donc échouer le passage nocturne entier.
            garmin_activity_id=None if r[1] is None else int(r[1]),
            storage_path=str(r[2]),
        )
        for r in rows
    )


def load_activity_summary(conn: DBConnection, activity_id: int) -> Mapping[str, Any] | None:
    """Résumé JSON d'UNE activité, extrait du dernier payload ``get_activities_by_date``.

    Garmin ne sert pas les résumés un par un : ``get_activities_by_date`` répond un
    TABLEAU d'activités pour une fenêtre de dates, stocké tel quel dans
    ``raw.api_response``. Retrouver une activité précise impose donc de déplier ce
    tableau (``jsonb_array_elements``) plutôt que de filtrer sur ``scope_key`` -- lequel
    reste NULL pour cet endpoint, faute d'une clé unique qui le caractériserait. Le coût
    est un parcours des lignes de cet endpoint ; il est assumé ici, à raison d'une
    requête par fichier en attente.

    ``ORDER BY fetched_at DESC LIMIT 1`` prend la version la PLUS RÉCENTE : la table est
    append-only et Garmin republie ses données (recalculs a posteriori). Prendre la plus
    ancienne figerait le résumé au premier état connu, ce qui irait exactement contre la
    raison d'être de cette table.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT elem "
            "FROM raw.api_response r, jsonb_array_elements(r.payload) AS elem "
            "WHERE r.endpoint = 'get_activities_by_date' "
            "  AND jsonb_typeof(r.payload) = 'array' "
            "  AND elem->>'activityId' = %s "
            "ORDER BY r.fetched_at DESC "
            "LIMIT 1",
            (str(activity_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = row[0]
    # psycopg décode déjà le jsonb en objet Python ; la branche str couvre une doublure
    # de test ou un pilote configuré autrement, sans trancher laquelle des deux est vraie.
    if isinstance(payload, str):
        decoded: Any = json.loads(payload)
        return decoded if isinstance(decoded, dict) else None
    return payload if isinstance(payload, dict) else None


def resolve_athlete_id(conn: DBConnection) -> int:
    """``athlete_id`` unique de ``core.athlete``.

    Lève si la table est vide ou en contient plusieurs, au lieu de retomber sur 1.
    Rattacher tout un historique au mauvais athlète produirait des seuils faux, donc
    des TSS faux, sans qu'aucune contrainte ne s'en aperçoive -- l'erreur ne se verrait
    qu'à la lecture des courbes, longtemps après.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT athlete_id FROM core.athlete ORDER BY athlete_id")
        rows = cur.fetchall()
    if len(rows) != 1:
        msg = (
            f"core.athlete contient {len(rows)} ligne(s) : impossible de choisir seul. "
            "Passer --athlete-id explicitement."
        )
        raise ValueError(msg)
    return int(rows[0][0])


def load_athlete_sex(conn: DBConnection, athlete_id: int) -> Sex:
    """``sex`` de l'athlète, requis par le TRIMP de Banister.

    Ce n'est pas une donnée d'état civil mais un coefficient de calcul : k vaut
    0.64/1.92 pour un homme et 0.86/1.67 pour une femme. Se tromper sous-estime ou
    surestime le TRIMP d'environ 15 % sur toute la charge, uniformément et sans
    aucune erreur visible -- exactement le genre de faux que rien ne rattrape.

    Lève si l'athlète est inconnu : ``--athlete-id`` mal saisi doit s'arrêter ici, pas
    rattacher un historique à une ligne qui n'existe pas.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT sex FROM core.athlete WHERE athlete_id = %s", (athlete_id,))
        row = cur.fetchone()
    if row is None:
        msg = f"core.athlete ne contient aucun athlete_id = {athlete_id}."
        raise ValueError(msg)
    value = str(row[0])
    if value not in ("male", "female"):
        # La contrainte CHECK de la migration 003 l'interdit ; si on arrive ici, c'est
        # que le schéma a divergé, et deviner serait pire que s'arrêter.
        msg = f"core.athlete.sex = {value!r} : valeur hors de ('male','female')."
        raise ValueError(msg)
    return cast("Sex", value)


def decode_archived_file(path: Path, *, activity_id: int) -> Any:
    """Choisit le décodeur d'après l'extension posée à l'archivage.

    Cette extension n'est pas une supposition sur le nom envoyé par Garmin :
    ``garminator.fit.archive`` renifle le contenu réel du membre d'archive et en DÉRIVE
    le suffixe. S'y fier ici revient donc à se fier à un fait constaté.
    """
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return decode_fit_file(path, activity_id=activity_id)
    if suffix == ".tcx":
        return decode_tcx_file(path, activity_id=activity_id)
    msg = f"extension inattendue {suffix!r} pour {path} : ni .fit ni .tcx."
    raise ValueError(msg)


def _mark_parsed(conn: DBConnection, fit_file_id: int) -> None:
    """Pose ``parsed_at`` / ``parser_version`` et EFFACE ``parse_error``.

    L'effacement compte : un fichier qui a échoué puis réussi ne doit pas conserver le
    message de son ancien échec, sous peine qu'un bilan de santé le signale
    éternellement comme problématique alors qu'il est en base et correct.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.fit_file "
            "SET parsed_at = now(), parser_version = %s, parse_error = NULL "
            "WHERE fit_file_id = %s",
            (PARSER_VERSION, fit_file_id),
        )
    conn.commit()


def _mark_failed(conn: DBConnection, fit_file_id: int, message: str) -> None:
    """Enregistre l'échec en laissant ``parsed_at`` à NULL.

    Le ``rollback()`` préalable est indispensable, et c'est la raison pour laquelle ce
    module exige ``rollback`` dans son protocole contrairement à ses voisins : après une
    erreur SQL, psycopg place la transaction en état « aborted » et refuse toute requête
    ultérieure. Sans annulation préalable, l'écriture même du message d'erreur
    échouerait -- et l'incident disparaîtrait sans laisser de trace, ce qui est
    exactement le contraire de ce qu'on cherche ici.

    Le message est tronqué : ``parse_error`` est un ``text`` sans limite, mais une trace
    de plusieurs kilo-octets par fichier rendrait la colonne illisible à la relecture
    (requête de diagnostic de ``docs/RUNBOOK.md``).
    """
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.fit_file SET parse_error = %s WHERE fit_file_id = %s",
            (message[:2000], fit_file_id),
        )
    conn.commit()


def transform_one(
    conn: DBConnection,
    pending: PendingFile,
    *,
    athlete_id: int,
    sex: Sex,
    thresholds: Sequence[Threshold],
) -> FileOutcome:
    """Transforme UN fichier. Ne laisse jamais échapper d'erreur propre à ce fichier.

    Un fichier indigeste ne doit pas emporter le passage entier : les autres séances
    n'ont rien à voir avec celle qui pose problème. L'erreur est donc capturée, inscrite
    dans ``raw.fit_file.parse_error`` et le passage continue. La colonne se relit avec la
    requête de diagnostic de ``docs/RUNBOOK.md`` ; ``format_transform_report`` en donne le
    total à chaque passage.

    La CAPTURE couvre la lecture du résumé et la pose de ``parsed_at``, pas seulement le
    calcul : un payload ``raw.api_response`` tronqué ou un incident sur l'``UPDATE`` final
    remonteraient autrement jusqu'à l'appelant, tuant le passage sans imprimer le moindre
    rapport -- l'opérateur ne saurait même pas où il s'est arrêté.
    """
    activity_id = pending.garmin_activity_id
    if activity_id is None:
        # Envoi manuel (migration 012). Le fichier porte pourtant de quoi le decrire : la
        # duree se deduit des deux formats, et le sport figure dans le message `session`
        # d'un FIT comme dans l'attribut `Sport` d'un TCX -- aucun lecteur ne les extrait
        # aujourd'hui. Ce qui manque vraiment est ailleurs : `build_activity_context`
        # n'existe qu'a partir d'un resume Garmin, et `core.activity.activity_id` EST
        # l'identifiant Garmin (cle primaire, sans sequence) -- un envoi manuel n'a donc
        # pas de cle. Le fichier RESTE dans la file plutot que d'etre marque en erreur :
        # le jour ou ces deux manques seront combles, il sera repris sans intervention.
        logger.info(
            "fichier %s : envoi manuel sans identifiant Garmin, transformation non prise "
            "en charge, fichier laissé dans la file.",
            pending.fit_file_id,
        )
        return FileOutcome(pending.fit_file_id, None, "skipped", "envoi manuel sans résumé")

    try:
        summary = load_activity_summary(conn, activity_id)
        if summary is None:
            # État transitoire, PAS une erreur : voir la docstring du module.
            logger.info(
                "activité %s : résumé get_activities_by_date absent, fichier %s reporté.",
                activity_id,
                pending.fit_file_id,
            )
            # La lecture ci-dessus a ouvert une transaction que ce chemin ne referme
            # sinon jamais : un lot entièrement reporté laisserait la session
            # `idle in transaction` et bloquerait l'avancée du VACUUM sur raw.*.
            conn.rollback()
            return FileOutcome(pending.fit_file_id, activity_id, "skipped", "résumé Garmin absent")

        context = build_activity_context(summary)
        decoded = decode_archived_file(Path(pending.storage_path), activity_id=activity_id)
        threshold, all_threshold = select_thresholds(
            thresholds, context.discipline, context.start_time_local.date()
        )
        bundle = build_core_rows(
            decoded,
            context,
            athlete_id=athlete_id,
            fit_file_id=pending.fit_file_id,
            threshold=threshold,
            all_threshold=all_threshold,
            sex=sex,
        )
        store_activity_bundle(conn, bundle)
        # Après le commit du bundle, jamais avant : voir « ordre d'écriture » en tête de
        # module.
        _mark_parsed(conn, pending.fit_file_id)
    except Exception as exc:  # isoler le fichier fautif, cf. docstring.
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("activité %s : transformation en échec -- %s", activity_id, message)
        _mark_failed(conn, pending.fit_file_id, message)
        return FileOutcome(pending.fit_file_id, activity_id, "failed", message)

    logger.info("activité %s écrite dans core (%s échantillons).", activity_id, len(bundle.samples))
    return FileOutcome(pending.fit_file_id, activity_id, "stored")


def transform_pending_files(
    conn: DBConnection,
    *,
    athlete_id: int | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    retry_failed: bool = False,
) -> TransformReport:
    """Traite un lot de fichiers en attente et renvoie le bilan du passage."""
    resolved_athlete = athlete_id if athlete_id is not None else resolve_athlete_id(conn)
    sex = load_athlete_sex(conn, resolved_athlete)
    # Les seuils sont chargés UNE fois pour tout le lot : ils sont historisés par période
    # de validité, pas par activité, et les relire à chaque fichier ferait des centaines
    # de requêtes identiques pour un résultat invariant.
    thresholds = load_thresholds(conn, athlete_id=resolved_athlete)
    pending = select_pending_files(conn, limit=limit, retry_failed=retry_failed)
    logger.info("%s fichier(s) en attente de transformation.", len(pending))
    outcomes = tuple(
        transform_one(conn, item, athlete_id=resolved_athlete, sex=sex, thresholds=thresholds)
        for item in pending
    )
    return TransformReport(outcomes=outcomes, failed_backlog=count_failed_files(conn))


def format_transform_report(report: TransformReport) -> str:
    """Rapport lisible sur stdout, dans la même veine que les autres points d'entrée.

    Les activités écrites ne sont pas détaillées ligne à ligne : sur un rattrapage de
    200 fichiers, seuls les cas anormaux méritent d'être lus.
    """
    lignes = [
        f"Transformation : {report.stored} écrite(s), {report.skipped} reportée(s), "
        f"{report.failed} en échec."
    ]
    lignes.extend(
        f"  activité {o.garmin_activity_id} -- {o.status} : {o.detail}"
        for o in report.outcomes
        if o.status != "stored"
    )
    if report.failed_backlog:
        lignes.append(
            f"ATTENTION : {report.failed_backlog} fichier(s) en echec hors de la file "
            "(voir docs/RUNBOOK.md section 10, puis --retry-failed)."
        )
    return "\n".join(lignes)
