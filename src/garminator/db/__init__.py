"""Garminator database access: versioned SQL migrations (L1 batch).

The runner (:mod:`garminator.db.runner`) is independent of :mod:`garminator.settings`:
it reads its own DSN (``GARMINATOR_POSTGRES_DSN`` environment variable or ``--dsn``
argument to the ``python -m garminator.db`` CLI) without ever requiring Garmin
credentials.
"""

from __future__ import annotations

from garminator.db.runner import (
    AppliedMigration,
    Migration,
    MigrationDriftError,
    MigrationError,
    Status,
    discover_migrations,
    migrate,
    status,
)

__all__ = [
    "AppliedMigration",
    "Migration",
    "MigrationDriftError",
    "MigrationError",
    "Status",
    "discover_migrations",
    "migrate",
    "status",
]
