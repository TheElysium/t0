"""Écriture dans ``raw.api_response`` et ``raw.ingest_run`` (migration ``002_raw.sql``).

Principe ELT « brut d'abord » (research 05 §1) : ce module ne fait **aucune**
transformation du payload — il le sérialise canoniquement (clés triées) pour le hash
d'intégrité, et l'écrit tel quel. Toute la logique de parsing/typage vit dans les lots
suivants (L3+), qui rejouent ``raw`` sans jamais re-solliciter Garmin.

Frontière DB choisie pour ce lot : les fonctions ci-dessous acceptent une connexion
typée par le protocole structurel :class:`DBConnection` (une simple vue « curseur +
commit » sur l'API psycopg 3), pas ``psycopg.Connection`` concrètement. Deux raisons :

1. :mod:`garminator.db` (lot L1) n'expose aucun utilitaire de requête générique — seulement
   le runner de migrations — donc il n'y a rien à réutiliser depuis là.
2. Typer contre un protocole structurel minimal permet aux tests d'injecter une fausse
   connexion en mémoire (aucun PostgreSQL requis) tout en restant vérifié par mypy,
   puisque ``psycopg.Connection`` satisfait ce protocole structurellement.

Ce module n'ouvre lui-même aucune connexion : c'est à l'appelant (le point d'entrée du
service, hors périmètre de ce lot) de construire la connexion réelle via
``psycopg.connect(settings.postgres_dsn.get_secret_value())`` et de la lui passer.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from datetime import date


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Vue structurelle minimale sur une connexion psycopg 3, pour l'injection en test."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def canonical_json_dumps(payload: object) -> str:
    """Sérialisation canonique : clés triées, séparateurs fixes — base du hash d'intégrité."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def payload_sha256(payload: object) -> bytes:
    """sha256 (32 octets bruts) du payload sérialisé canoniquement.

    Correspond à ``raw.api_response.payload_sha256`` (``CHECK length = 32``).
    """
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).digest()


def insert_api_response(
    conn: DBConnection,
    *,
    endpoint: str,
    payload: object,
    http_status: int = 200,
    scope_date: date | None = None,
    scope_key: str | None = None,
    ingest_run_id: int | None = None,
) -> int:
    """Insère une ligne dans ``raw.api_response`` et retourne son ``id``.

    ``http_status`` vaut 200 par défaut : ``garminconnect`` 0.3.2 n'expose pas le code
    HTTP réel d'un appel réussi (il désérialise directement le JSON), donc ce module ne
    peut affirmer que le succès logique de l'appel — décision assumée, documentée dans
    le rapport de lot plutôt que masquée derrière une valeur inventée sans commentaire.
    """
    sha = payload_sha256(payload)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.api_response
                (endpoint, scope_date, scope_key, http_status, payload, payload_sha256,
                 ingest_run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                endpoint,
                scope_date,
                scope_key,
                http_status,
                Jsonb(payload),
                sha,
                ingest_run_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError("INSERT raw.api_response n'a retourné aucune ligne (RETURNING id).")
    return int(row[0])


class FitFileShaConflictError(RuntimeError):
    """``sha256`` déjà associé en base à une activité DIFFÉRENTE de celle demandée.

    « Différente » compare aussi la provenance : ``None`` (envoi manuel, migration
    ``012_raw_fit_file_manual_upload.sql``) n'est jamais confondu avec un
    ``garmin_activity_id`` réel. Un même contenu binaire réutilisé entre un envoi manuel
    et une activité Garmin authentique — ou entre deux activités Garmin distinctes — est
    exactement le genre de collision que cette exception rend visible plutôt que de
    laisser une relecture silencieuse réattribuer la ligne à la mauvaise provenance.

    Scénario réel identifié en revue (lot L12, cet athlète est triathlète) : une séance
    brick/multisport produit une activité parente et des activités enfants (course, vélo,
    natation), chacune avec son propre ``garmin_activity_id`` — et le téléchargement
    « original » de plusieurs de ces ids peut servir EXACTEMENT le même fichier de
    session (mêmes octets, donc même sha256). Autre cas plausible : un placeholder
    d'erreur identique servi par Garmin pour deux activités indisponibles.

    Sans cette détection, :func:`insert_fit_file` retournerait silencieusement le
    ``fit_file_id`` de l'AUTRE activité (celle qui a gagné la course à l'insertion) :
    aucune ligne ne référencerait alors l'activité demandée ici, son fichier fraîchement
    archivé deviendrait un orphelin permanent, et un appelant qui construirait dessus un
    ``fit_file_id`` de bonne foi pointerait vers une mauvaise activité (un L3 qui y
    poserait ``parsed_at`` marquerait la ligne d'une autre activité que celle réellement
    parsée). Cette exception rend le cas visible plutôt que de mentir sur le statut.

    Limite assumée, documentée plutôt que masquée : la vraie correction de fond serait de
    permettre plusieurs lignes ``raw.fit_file`` pour un même ``sha256`` (une par activité
    qui le référence), ce qui suppose de retirer ``fit_file_sha_uq`` — une modification
    du DDL figé (``migrations/002_raw.sql``), hors du périmètre de ce lot. Le minimum
    exigé ici est de ne jamais prétendre qu'une insertion a réussi quand elle a en fait
    été refusée pour le mauvais motif.
    """


def insert_fit_file(
    conn: DBConnection,
    *,
    garmin_activity_id: int | None,
    storage_path: str,
    sha256: bytes,
    size_bytes: int,
) -> int:
    """Insère une ligne dans ``raw.fit_file`` et retourne son ``fit_file_id``.

    Appelée par :mod:`garminator.scheduler.activity_files` (lot L12) **après**, jamais
    avant, que le fichier ait été écrit et vérifié sur le PVC par
    :func:`garminator.fit.archive.archive_download` — cette fonction ne touche jamais le
    volume, elle se contente d'enregistrer un fait déjà établi sur disque. Également
    appelée par :func:`garminator.api.ingest.ingest_upload` (lot L7) pour un envoi
    manuel, avec ``garmin_activity_id=None`` — voir
    ``migrations/012_raw_fit_file_manual_upload.sql``, qui a rendu la colonne nullable
    précisément pour ce second appelant et a fixé la convention : ``None`` ici signifie
    « envoi manuel », jamais autre chose.

    Gestion du conflit sur ``sha256`` (``fit_file_sha_uq``, ``UNIQUE``) : un même
    contenu binaire retéléchargé pour la MÊME activité (cas courant : ``run_scheduled_poll``
    du lot L6 re-sonde systématiquement le jour courant, donc ce module sera rappelé
    plusieurs fois sur les mêmes activités récentes) signifie « déjà archivée » — la
    relecture après conflit retourne alors l'``id`` déjà existant, sans dupliquer ni
    écraser de ligne. Le choix reste ``DO NOTHING`` plutôt que ``DO UPDATE`` : une ligne
    ``raw.fit_file`` existante ne doit **jamais** être réécrite silencieusement (elle
    porte potentiellement déjà ``parsed_at``/``parser_version`` posés par L3 — un
    ``DO UPDATE`` naïf sur les seules colonnes de ce module les écraserait avec des
    ``NULL``). Un envoi manuel identique renvoyé deux fois (même sha256, les deux avec
    ``garmin_activity_id=None``) suit exactement le même chemin : ``None == None`` est
    un accord de provenance, pas une divergence.

    Un même contenu binaire attaché à une AUTRE activité (``garmin_activity_id``
    différent, ``None`` compris) n'est plus traité comme le même cas — voir
    :class:`FitFileShaConflictError` pour le scénario multisport qui a motivé cette
    distinction, identifié en revue, désormais étendu à la distinction Garmin/envoi
    manuel. La relecture après conflit compare désormais l'activité (ou son absence),
    pas seulement le sha256.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.fit_file
                (garmin_activity_id, storage_path, sha256, size_bytes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (sha256) DO NOTHING
            RETURNING fit_file_id;
            """,
            (garmin_activity_id, storage_path, sha256, size_bytes),
        )
        inserted_row = cur.fetchone()
        conflict_row: tuple[Any, ...] | None = None
        if inserted_row is None:
            # Conflit sur sha256 : la ligne existe déjà -- mais potentiellement pour une
            # AUTRE activité (brick/multisport, voir FitFileShaConflictError), donc la
            # relecture doit porter aussi sur garmin_activity_id, pas seulement sur
            # fit_file_id comme avant la revue.
            cur.execute(
                "SELECT fit_file_id, garmin_activity_id FROM raw.fit_file WHERE sha256 = %s;",
                (sha256,),
            )
            conflict_row = cur.fetchone()
    conn.commit()

    if inserted_row is not None:
        return int(inserted_row[0])

    if conflict_row is None:
        raise RuntimeError(
            "INSERT raw.fit_file en conflit sur sha256 mais aucune ligne existante "
            "retrouvée par relecture : incohérence inattendue entre les deux requêtes."
        )
    existing_fit_file_id = int(conflict_row[0])
    # existing_activity_id N'EST PAS forcé en int : la ligne relue peut elle-même être un
    # envoi manuel (NULL en base). int(None) lèverait ici exactement le défaut que ce lot
    # corrige côté transform.runner -- ne pas le reproduire ici.
    existing_activity_id: int | None = None if conflict_row[1] is None else int(conflict_row[1])
    if existing_activity_id != garmin_activity_id:
        existing_label = (
            "envoi manuel" if existing_activity_id is None else str(existing_activity_id)
        )
        requested_label = "envoi manuel" if garmin_activity_id is None else str(garmin_activity_id)
        raise FitFileShaConflictError(
            f"sha256 déjà associé à l'activité {existing_label} "
            f"(fit_file_id={existing_fit_file_id}) ; insertion refusée pour l'activité "
            f"{requested_label} -- contenu identique servi pour deux provenances "
            "différentes (séance multisport/brick partageant un même fichier de "
            "session, placeholder d'erreur identique, ou envoi manuel d'un fichier déjà "
            "archivé depuis Garmin). Aucune ligne n'a été créée pour cette demande."
        )
    return existing_fit_file_id


def start_ingest_run(
    conn: DBConnection,
    *,
    mode: str,
    code_version: str | None = None,
) -> int:
    """Ouvre une ligne ``raw.ingest_run`` (statut ``running``) et retourne son id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.ingest_run (mode, status, code_version)
            VALUES (%s, 'running', %s)
            RETURNING ingest_run_id;
            """,
            (mode, code_version),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError(
            "INSERT raw.ingest_run n'a retourné aucune ligne (RETURNING ingest_run_id)."
        )
    return int(row[0])


def finish_ingest_run(
    conn: DBConnection,
    *,
    ingest_run_id: int,
    status: str,
    notes: dict[str, Any] | None = None,
) -> None:
    """Clôture une ligne ``raw.ingest_run`` : ``finished_at``, ``status``, ``notes``.

    ``status`` doit être l'une des valeurs autorisées par la contrainte
    ``ingest_run_status_ck`` (``ok``, ``partial``, ``failed``) — ``running`` n'est jamais
    un statut de clôture valide, ce module ne le valide pas lui-même : c'est PostgreSQL
    qui refuse via la contrainte ``CHECK``, à dessein (une seule source de vérité pour
    les valeurs permises).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw.ingest_run
               SET finished_at = now(), status = %s, notes = %s
             WHERE ingest_run_id = %s;
            """,
            (status, Jsonb(notes) if notes is not None else None, ingest_run_id),
        )
    conn.commit()
