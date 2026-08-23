"""Accès base de données de garminator : migrations SQL versionnées (lot L1).

Le runner (:mod:`garminator.db.runner`) est indépendant de :mod:`garminator.settings` : il
lit son propre DSN (variable d'environnement ``GARMINATOR_POSTGRES_DSN`` ou argument ``--dsn``
de la CLI ``python -m garminator.db``) sans jamais exiger d'identifiants Garmin.
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
