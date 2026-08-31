"""Read-only database access for the MCP server.

Dedicated role ``t0_mcp_ro``, never the ingest credentials (HANDOFF-MCP §3): the
DSN arrives from its own Secret via :class:`t0.settings.Settings`. Defense in
depth against a mis-granted role: the connection itself is opened
``default_transaction_read_only = on``, so even a bug in this codebase cannot
issue a mutating statement -- the read-only guarantee does not rest on grants
alone. ``statement_timeout`` is set per connection through connection options:
tool output is unbounded by design, so a runaway query must be cut by the
server, not by a client-side timeout racing the event loop.

Sync psycopg + :mod:`psycopg_pool`, executed through ``asyncio.to_thread`` by
the tool layer. The MCP is single-user (one athlete, one CLI client at human
question rate): a small pool and blocking calls in worker threads are the
simplest correct shape -- no async psycopg driver, no event-loop-aware pool.

No exception text ever carries the DSN: psycopg error messages expose host and
user names but never the password; still, everything crossing the tool boundary
is reduced to a generic :class:`QueryError`, and the rich exception only goes to
the server log (which a client never reads).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5

#: Server-side guards applied to EVERY connection of the pool, via libpq
#: connection options (there is no "SET" surviving pool recycling reliably --
#: options are part of the connection itself).
_CONNECTION_OPTIONS = "default_transaction_read_only=on"


class QueryError(RuntimeError):
    """A read-only query failed. Carries no DSN fragment, ever."""


class SqlRunner(Protocol):
    """What the pure handlers need from the database: rows in, dicts out.

    Handlers depend on this Protocol, not on :class:`Reader` -- tests inject a
    fake runner, and the handlers stay transport- and driver-independent.
    """

    def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> dict[str, Any] | None: ...


class Reader:
    """Connection pool over the read-only role, opened lazily on first use.

    Lazy open matters for tests and for the health probes: building the ASGI app
    must not attempt a database connection (the Kubernetes probe decides
    readiness itself; tests without a database never trigger one).
    """

    def __init__(self, dsn: str, statement_timeout_ms: int) -> None:
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    def _connection_pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None:
                options = (
                    f"-c statement_timeout={self._statement_timeout_ms} "
                    f"-c {_CONNECTION_OPTIONS}"
                )
                self._pool = ConnectionPool(
                    conninfo=self._dsn,
                    min_size=1,
                    max_size=4,
                    open=True,
                    kwargs={"connect_timeout": CONNECT_TIMEOUT_S, "options": options},
                )
                logger.info(
                    "MCP read-only pool opened (statement_timeout=%dms).",
                    self._statement_timeout_ms,
                )
            return self._pool

    def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Run a SELECT, return rows as dicts."""
        try:
            with (
                self._connection_pool().connection() as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute(sql, params)
                rows: list[dict[str, Any]] = cur.fetchall()
                return rows
        except psycopg.Error as exc:
            logger.warning("MCP query failed: %s", exc)
            raise QueryError("database query failed") from None

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """Run a SELECT expected to return at most one row."""
        try:
            with (
                self._connection_pool().connection() as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute(sql, params)
                row: dict[str, Any] | None = cur.fetchone()
                return row
        except psycopg.Error as exc:
            logger.warning("MCP query failed: %s", exc)
            raise QueryError("database query failed") from None

    def check(self) -> None:
        """Probe the connection path: SELECT 1. Raises on any failure.

        Bounded by the pool's connect_timeout: a half-open TCP connection cannot
        hold the readiness probe past its own timeout.
        """
        try:
            with self._connection_pool().connection() as conn:
                conn.execute("SELECT 1")
        except psycopg.Error as exc:
            logger.warning("MCP readiness check failed: %s", exc)
            raise QueryError("database unreachable") from None

    def close(self) -> None:
        """Close the pool if it was opened; a no-op otherwise (tests)."""
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
