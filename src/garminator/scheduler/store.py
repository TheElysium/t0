"""Écriture/lecture de ``raw.poll_window`` et ``raw.payload_seen`` (migrations 002 et 009).

Même choix de frontière DB que :mod:`garminator.garmin.raw_store` (dont ce module
s'inspire délibérément, sans lui être couplé) : les fonctions ci-dessous acceptent une
connexion typée par un protocole structurel minimal (:class:`DBConnection`), pas
``psycopg.Connection`` concrètement -- ce qui permet aux tests d'injecter une fausse
connexion en mémoire, sans PostgreSQL, tout en restant vérifié par mypy puisqu'une vraie
connexion psycopg 3 satisfait ce protocole.

``DBConnection`` est redéfini ici (plutôt que réimporté de ``garminator.garmin.raw_store``)
parce que ce module a en plus besoin de ``fetchall`` -- protocole structurel donc
composition naturelle, pas de duplication de logique.

Ce module n'ouvre lui-même aucune connexion et ne fait aucun appel réseau.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from garminator.scheduler.planning import WindowState

if TYPE_CHECKING:
    from datetime import date


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Vue structurelle minimale sur une connexion psycopg 3, pour l'injection en test."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def _normalize_scope_key(scope_key: str | None) -> str:
    """``NULL`` -> ``''`` : identique au ``DEFAULT ''`` de ``raw.payload_seen.scope_key``
    (migration 002), nécessaire car la colonne est ``NOT NULL`` (elle participe à la clé
    primaire). Ce n'est PAS un zéro déguisé en absence : c'est la même sentinelle que le
    DDL figé utilise déjà pour « sans discriminant secondaire », documentée là-bas."""
    return scope_key if scope_key is not None else ""


def load_window_states(conn: DBConnection, *, endpoint: str) -> dict[date, WindowState]:
    """Lit l'état connu de toutes les fenêtres de ``endpoint`` dans ``raw.poll_window``.

    Une date absente du résultat signifie « jamais tentée » -- c'est
    :func:`garminator.scheduler.planning.due_windows` qui l'interprète comme due.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT window_date, status, attempts, last_attempted_at, last_error "
            "FROM raw.poll_window WHERE endpoint = %s ORDER BY window_date;",
            (endpoint,),
        )
        rows = cur.fetchall()
    return {
        row[0]: WindowState(
            status=row[1], attempts=row[2], last_attempted_at=row[3], last_error=row[4]
        )
        for row in rows
    }


def begin_window(
    conn: DBConnection, *, endpoint: str, window_date: date, ingest_run_id: int | None
) -> None:
    """Marque une fenêtre ``in_progress`` AVANT tout appel réseau, incrémente ``attempts``.

    Appelée avant le sondage réel : si le processus meurt pendant celui-ci, la ligne
    reste ``in_progress`` -- ni ``done`` (ce qui masquerait la perte), ni ``failed`` (ce
    qui exigerait d'avoir déjà capturé l'exception). C'est
    :func:`~garminator.scheduler.planning.due_windows` qui, au run suivant, reconnaît une
    ligne ``in_progress`` périmée comme un run interrompu à rejouer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.poll_window
                (endpoint, window_date, status, attempts, last_attempted_at, last_ingest_run_id)
            VALUES (%s, %s, 'in_progress', 1, now(), %s)
            ON CONFLICT (endpoint, window_date) DO UPDATE
                SET status = 'in_progress',
                    attempts = raw.poll_window.attempts + 1,
                    last_attempted_at = now(),
                    last_ingest_run_id = EXCLUDED.last_ingest_run_id;
            """,
            (endpoint, window_date, ingest_run_id),
        )
    conn.commit()


def complete_window(conn: DBConnection, *, endpoint: str, window_date: date) -> None:
    """Marque une fenêtre ``done`` -- appelée seulement après un sondage intégralement réussi.

    Efface ``last_error`` : une erreur d'une tentative antérieure ne doit jamais rester
    affichée à côté d'un succès plus récent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.poll_window SET status = 'done', completed_at = now(), "
            "last_error = NULL WHERE endpoint = %s AND window_date = %s;",
            (endpoint, window_date),
        )
    conn.commit()


def fail_window(conn: DBConnection, *, endpoint: str, window_date: date, error: str) -> None:
    """Marque une fenêtre ``failed`` avec son message d'erreur -- rejouable au run suivant."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.poll_window SET status = 'failed', last_error = %s "
            "WHERE endpoint = %s AND window_date = %s;",
            (error, endpoint, window_date),
        )
    conn.commit()


def has_payload_been_seen(
    conn: DBConnection,
    *,
    endpoint: str,
    scope_date: date,
    scope_key: str | None,
    payload_sha256: bytes,
) -> bool:
    """``True`` si ce contenu exact (même hash) a déjà été vu pour ``(endpoint, scope_date,
    scope_key)`` -- interroge ``raw.payload_seen`` (migration 002), sans jamais y écrire."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM raw.payload_seen WHERE endpoint = %s AND scope_date = %s "
            "AND scope_key = %s AND payload_sha256 = %s;",
            (endpoint, scope_date, _normalize_scope_key(scope_key), payload_sha256),
        )
        row = cur.fetchone()
    return row is not None


def record_payload_seen(
    conn: DBConnection,
    *,
    endpoint: str,
    scope_date: date,
    scope_key: str | None,
    payload_sha256: bytes,
) -> None:
    """Enregistre (ou republie) ce contenu dans ``raw.payload_seen``.

    ``ON CONFLICT ... DO UPDATE`` incrémentant ``seen_count`` : exactement le motif
    documenté par le ``COMMENT ON TABLE raw.payload_seen`` de la migration 002. Idempotent
    par construction : appeler deux fois avec le même hash ne crée jamais de doublon, se
    contente d'incrémenter le compteur de republications.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.payload_seen (endpoint, scope_date, scope_key, payload_sha256)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (endpoint, scope_date, scope_key, payload_sha256) DO UPDATE
                SET last_seen_at = now(),
                    seen_count = raw.payload_seen.seen_count + 1;
            """,
            (endpoint, scope_date, _normalize_scope_key(scope_key), payload_sha256),
        )
    conn.commit()
