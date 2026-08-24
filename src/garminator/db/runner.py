"""SQL migration runner for garminator (L1 batch).

Principles that govern this module:

1. **Independent of :mod:`garminator.settings`.** PostgreSQL DSN is read independently
   (dedicated environment variable or explicit argument), never constructing the full
   :class:`~garminator.settings.Settings` object — that requires Garmin credentials that
   a migration task (or a Kubernetes ``CronJob`` that only migrates schema) has no reason
   to possess.
2. **Forward only.** No automatic rollback is invented: an applied migration cannot be
   undone by this module. An incident is fixed by a corrective migration *next*, never by
   rewriting history.
3. **One migration = one transaction.** The entire file is sent in one request (psycopg
   simple protocol, active while no parameters are bound — see "Differences from
   psycopg2" docs), so all instructions succeed or fail together.
4. **Refuse on drift.** If the SHA-256 checksum of an already-applied migration has
   changed since application, or its file vanished from the migrations directory, the
   runner refuses to start — before even attempting to apply a pending migration.
5. **Only one runner at a time.** A PostgreSQL session advisory lock (:data:`ADVISORY_LOCK_KEY`)
   is acquired before any state read. Without it, two concurrent runners (two ``CronJob``s,
   or a deployment during manual migration) read the same "pending" state and attempt the
   same migration: the second fails mid-DDL with a ``public.schema_migration`` primary key
   violation or duplicate ``CREATE TABLE``, in a partially-applied state. The lock is
   taken in *try* mode (non-blocking): a second runner fails immediately and cleanly
   rather than blocking indefinitely in a container.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    from datetime import datetime

#: Module logger. NEVER logs the DSN: it bears the PostgreSQL password.
logger = logging.getLogger(__name__)

#: Name of the environment variable bearing the PostgreSQL DSN used by this runner.
#: Chosen identical to the variable name read by ``garminator.settings.Settings``
#: (``GARMINATOR_`` prefix) so an operator has only one value to set, but this module
#: reads it itself, without ever importing or constructing ``Settings``.
DSN_ENV_VAR = "GARMINATOR_POSTGRES_DSN"

#: Default migrations directory: ``<repository root>/migrations``.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

#: Table tracking applied migrations. Deliberately in ``public`` (schema always present,
#: even before migration 001 creates ``raw``/``core``/``mart``) rather than in one of
#: the application schemas.
_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migration (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text         NOT NULL,
    applied_at  timestamptz  NOT NULL DEFAULT now()
);
"""

_FILENAME_RE = re.compile(r"^(?P<version>\d+)_(?P<name>.+)\.sql$")

#: PostgreSQL advisory lock key protecting the entire migration sequence. Arbitrary but
#: STABLE value: two processes coordinate only if they use the same one. Session-scoped
#: (``pg_advisory_lock``, not ``pg_advisory_xact_lock``) because the runner chains
#: multiple transactions — one per migration — and the lock must cover the entire
#: sequence, not each transaction in isolation.
ADVISORY_LOCK_KEY = 4_071_128_301_055_216_401


class MigrationError(RuntimeError):
    """Generic migration runner error."""


class MigrationDriftError(MigrationError):
    """An already-applied migration has a different checksum than the file on disk.

    Raised *before* attempting to apply any pending migration: the runner refuses to
    start until drift is resolved by a human (file corrected, or incident documented).
    """


class MigrationLockError(MigrationError):
    """Another migration runner already holds the advisory lock.

    Raised immediately without waiting: a second process must neither apply the same
    migrations in parallel nor block indefinitely in a container.
    """


@dataclass(frozen=True, slots=True)
class Migration:
    """A versioned migration as found on disk."""

    version: int
    name: str
    path: Path
    checksum: str

    @property
    def sql(self) -> str:
        """Full SQL content of the file, read on each access (no in-memory cache)."""
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A row from ``public.schema_migration``."""

    version: int
    name: str
    checksum: str
    applied_at: datetime


def compute_checksum(path: Path) -> str:
    """SHA-256 checksum (hex) of raw file bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations(migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> list[Migration]:
    """List migrations from directory, sorted by version number.

    Files must follow the pattern ``NNN_name.sql`` (``NNN`` numeric, zero-padded or not).
    Any ``.sql`` file not matching this pattern is silently ignored (it's not part of
    the migration chain: e.g., a test fixture).

    Raises:
        MigrationError: if two files declare the same version number.
    """
    migrations: dict[int, Migration] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        if version in migrations:
            raise MigrationError(
                f"Migration version {version} present twice: "
                f"{migrations[version].path.name} and {path.name}."
            )
        migrations[version] = Migration(
            version=version,
            name=match.group("name"),
            path=path,
            checksum=compute_checksum(path),
        )
    return [migrations[v] for v in sorted(migrations)]


def detect_drift(migrations: list[Migration], applied: list[AppliedMigration]) -> list[str]:
    """Compare applied migrations in database to files on disk.

    Returns list of drift messages (empty if all match). Drift covers two cases: checksum
    changed, or an applied migration's file vanished from the directory — in both cases
    we can no longer guarantee that what was applied matches what the repository claims
    today.
    """
    by_version = {m.version: m for m in migrations}
    issues: list[str] = []
    for row in applied:
        current = by_version.get(row.version)
        if current is None:
            issues.append(
                f"Migration {row.version} ({row.name}) applied to database on "
                f"{row.applied_at.isoformat()} but its file vanished from the migrations "
                "directory."
            )
        elif current.checksum != row.checksum:
            issues.append(
                f"Migration {row.version} ({row.name}): checksum changed since application "
                f"({row.checksum[:12]}… in database, {current.checksum[:12]}… on disk). An "
                "applied migration must not be modified: fix by writing a new migration."
            )
    return issues


def plan_pending(migrations: list[Migration], applied_versions: set[int]) -> list[Migration]:
    """Migrations not yet applied, in the order they should be applied."""
    return [m for m in migrations if m.version not in applied_versions]


def resolve_dsn(dsn: str | None) -> str:
    """Resolve the DSN to use: explicit argument, otherwise environment variable.

    Never reads ``garminator.settings``: this module must work on a machine where only
    PostgreSQL is configured (no Garmin credentials).

    Raises:
        MigrationError: if no DSN is available from either source.
    """
    if dsn:
        return dsn
    value = os.environ.get(DSN_ENV_VAR)
    if not value:
        raise MigrationError(f"No PostgreSQL DSN provided: pass --dsn, or set {DSN_ENV_VAR}.")
    return value


def connect(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    """Open a psycopg 3 connection in explicit transactional mode (autocommit=False)."""
    return psycopg.connect(dsn, autocommit=False)


def acquire_lock(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Acquire session advisory lock, or fail immediately if already held.

    Raises:
        MigrationLockError: if another runner holds the lock.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
    conn.commit()
    if row is None or not row[0]:
        raise MigrationLockError(
            "Another migration runner already holds advisory lock "
            f"{ADVISORY_LOCK_KEY} on this database. No migrations were applied."
        )


def release_lock(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Release the advisory lock. No effect if lock is not held.

    Called from ``finally``: must never mask the original exception. Current transaction
    is first rolled back (after a failed migration, the connection may be in "aborted
    transaction" state where any command fails), and unlock failure is ignored — closing
    the connection releases the session lock on the server side anyway.
    """
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (ADVISORY_LOCK_KEY,))
        conn.commit()
    except psycopg.Error:
        logger.warning(
            "Cannot unlock advisory lock %s; it will be released when connection closes.",
            ADVISORY_LOCK_KEY,
        )


def ensure_tracking_table(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Create ``public.schema_migration`` if it doesn't exist, in its own transaction."""
    with conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE_DDL)
    conn.commit()


def fetch_applied(conn: psycopg.Connection[tuple[Any, ...]]) -> list[AppliedMigration]:
    """Read current state of ``public.schema_migration``, sorted by version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, name, checksum, applied_at FROM public.schema_migration "
            "ORDER BY version;"
        )
        rows = cur.fetchall()
    return [AppliedMigration(version=v, name=n, checksum=c, applied_at=a) for v, n, c, a in rows]


def apply_migration(conn: psycopg.Connection[tuple[Any, ...]], migration: Migration) -> None:
    """Apply a migration in its own transaction (implicit BEGIN / COMMIT / ROLLBACK).

    The entire file is sent without bound parameters: psycopg 3 then uses the simple
    protocol and executes all instructions from the file, separated by ``;``, as a single
    batch — this is what gives us "one migration = one transaction" without manual
    statement splitting.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO public.schema_migration (version, name, checksum) "
                "VALUES (%s, %s, %s);",
                (migration.version, migration.name, migration.checksum),
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def migrate(
    dsn: str | None = None, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Migration]:
    """Apply all pending migrations, in order, after drift check.

    Returns:
        List of migrations actually applied in this call (empty if database was already
        up to date — this makes re-running "zero change").

    Raises:
        MigrationLockError: if another runner already holds the advisory lock. Nothing
            is applied: the second process fails immediately without waiting.
        MigrationDriftError: if an already-applied migration no longer matches the file
            on disk. Nothing is applied in this case: refusal is total, not partial.
    """
    resolved_dsn = resolve_dsn(dsn)
    migrations = discover_migrations(migrations_dir)
    conn = connect(resolved_dsn)
    try:
        acquire_lock(conn)
        try:
            ensure_tracking_table(conn)
            applied = fetch_applied(conn)
            drift = detect_drift(migrations, applied)
            if drift:
                raise MigrationDriftError("Runner refuses to start: " + " | ".join(drift))
            applied_versions = {row.version for row in applied}
            pending = plan_pending(migrations, applied_versions)
            for migration in pending:
                apply_migration(conn, migration)
            return pending
        finally:
            release_lock(conn)
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class Status:
    """Current migration state, for the ``status`` command."""

    applied: list[AppliedMigration]
    pending: list[Migration]
    drift: list[str]


def status(dsn: str | None = None, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> Status:
    """Report current state without applying or modifying anything."""
    resolved_dsn = resolve_dsn(dsn)
    migrations = discover_migrations(migrations_dir)
    conn = connect(resolved_dsn)
    try:
        ensure_tracking_table(conn)
        applied = fetch_applied(conn)
        drift = detect_drift(migrations, applied)
        applied_versions = {row.version for row in applied}
        pending = plan_pending(migrations, applied_versions)
        return Status(applied=applied, pending=pending, drift=drift)
    finally:
        conn.close()


def format_status(report: Status) -> str:
    """Plain text rendering of :class:`Status`, used by the CLI."""
    lines = ["Applied migrations:"]
    if report.applied:
        for row in report.applied:
            lines.append(f"  [{row.version:03d}] {row.name} — {row.applied_at.isoformat()}")
    else:
        lines.append("  (none)")
    lines.append("Pending migrations:")
    if report.pending:
        for m in report.pending:
            lines.append(f"  [{m.version:03d}] {m.name}")
    else:
        lines.append("  (none)")
    if report.drift:
        lines.append("Drift detected:")
        for issue in report.drift:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)
