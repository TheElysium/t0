"""Read-only database access for the MCP server (role t0_mcp_ro)."""

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

# Connection options, not SET: options are part of the connection itself and
# survive pool recycling.
_CONNECTION_OPTIONS = "default_transaction_read_only=on"


class QueryError(RuntimeError):
    """A read-only query failed. Carries no DSN fragment, ever."""


class SqlRunner(Protocol):
    """What the handlers need from the database: rows in, dicts out."""

    def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> dict[str, Any] | None: ...


class Reader:
    """Connection pool over the read-only role, opened lazily on first use."""

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
        """Probe the connection path: SELECT 1, bounded by the connect timeout."""
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
