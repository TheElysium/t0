"""T0 database access: versioned SQL migrations (L1 batch).

The runner (:mod:`t0.db.runner`) is independent of :mod:`t0.settings`:
it reads its own DSN (``T0_POSTGRES_DSN`` environment variable or ``--dsn``
argument to the ``python -m t0.db`` CLI) without ever requiring Garmin
credentials.
"""

from __future__ import annotations

from t0.db.runner import (
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
