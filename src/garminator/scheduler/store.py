"""Read/write ``raw.poll_window`` and ``raw.payload_seen`` (migrations 002 and 009).

Same DB boundary choice as :mod:`garminator.garmin.raw_store` (which this module
deliberately mimics without coupling to): functions below accept connections typed
by a minimal structural protocol (:class:`DBConnection`), not ``psycopg.Connection``
concretely—which lets tests inject a fake in-memory connection, no PostgreSQL, while
staying verified by mypy since a real psycopg 3 connection satisfies the protocol.

``DBConnection`` is redefined here (not re-imported from ``garminator.garmin.raw_store``)
because this module additionally needs ``fetchall``—structural protocol so natural
composition, no logic duplication.

This module opens no connections itself and makes no network calls.
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
    """Minimal structural view of a psycopg 3 connection, for test injection."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def _normalize_scope_key(scope_key: str | None) -> str:
    """``NULL`` → ``''``: identical to ``DEFAULT ''`` of ``raw.payload_seen.scope_key``
    (migration 002), needed because the column is ``NOT NULL`` (participates in primary
    key). Not zero disguised as absence: it is the same sentinel the frozen DDL already
    uses for "no secondary discriminant", documented there."""
    return scope_key if scope_key is not None else ""


def load_window_states(conn: DBConnection, *, endpoint: str) -> dict[date, WindowState]:
    """Load known state of all ``endpoint`` windows from ``raw.poll_window``.

    A missing date in the result means "never attempted"—it is
    :func:`garminator.scheduler.planning.due_windows` that interprets it as due.
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
    """Mark a window ``in_progress`` BEFORE any network call, increment ``attempts``.

    Called before actual poll: if process dies during it, the row stays ``in_progress``—
    neither ``done`` (which would hide loss) nor ``failed`` (which would require already
    having caught the exception). It is :func:`~garminator.scheduler.planning.due_windows`
    that on the next run recognizes a stale ``in_progress`` row as an interrupted run
    to replay.
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
    """Mark a window ``done``—called only after a fully successful poll.

    Clears ``last_error``: an error from a prior attempt must never remain
    displayed alongside a more recent success.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.poll_window SET status = 'done', completed_at = now(), "
            "last_error = NULL WHERE endpoint = %s AND window_date = %s;",
            (endpoint, window_date),
        )
    conn.commit()


def fail_window(conn: DBConnection, *, endpoint: str, window_date: date, error: str) -> None:
    """Mark a window ``failed`` with its error message—replayable next run."""
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
    """``True`` if this exact content (same hash) has been seen for ``(endpoint,
    scope_date, scope_key)``—queries ``raw.payload_seen`` (migration 002), never writes."""
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
    """Record (or republish) this content in ``raw.payload_seen``.

    ``ON CONFLICT ... DO UPDATE`` incrementing ``seen_count``: exactly the pattern
    documented by the ``COMMENT ON TABLE raw.payload_seen`` in migration 002.
    Idempotent by design: calling twice with the same hash never creates a duplicate,
    merely increments the rebroadcast counter.
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
