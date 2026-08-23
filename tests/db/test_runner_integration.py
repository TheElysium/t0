"""Tests d'intégration du runner de migrations contre un vrai PostgreSQL.

Ces tests ne s'exécutent que si un PostgreSQL est réellement joignable via
``GARMINATOR_TEST_POSTGRES_DSN`` (une base *dédiée aux tests*, jamais la base
applicative -- chaque test la nettoie via ``DROP SCHEMA ... CASCADE`` avant de la
repeupler). Absent cette variable, ils sont ignorés (``pytest.skip``) plutôt qu'en échec :
au moment du lot L1, aucun PostgreSQL local ni conteneur Docker n'était disponible dans
l'environnement d'exécution (voir le rapport de fin de lot) -- ce fichier reste prêt à
s'exécuter dès qu'une base de test existera (CI, ou poste de développement).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from garminator.db.runner import (
    ADVISORY_LOCK_KEY,
    MigrationDriftError,
    MigrationLockError,
    discover_migrations,
    migrate,
    status,
)

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
TEST_DSN_ENV_VAR = "GARMINATOR_TEST_POSTGRES_DSN"


def _require_test_dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV_VAR} non défini : pas de PostgreSQL de test disponible "
            "(voir le rapport de fin de lot L1)."
        )
    return dsn


@pytest.fixture
def clean_dsn() -> str:
    """DSN vers une base de test, réinitialisée (schémas applicatifs + table de suivi)."""
    import psycopg

    dsn = _require_test_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS raw CASCADE;")
        cur.execute("DROP SCHEMA IF EXISTS core CASCADE;")
        cur.execute("DROP SCHEMA IF EXISTS mart CASCADE;")
        cur.execute("DROP TABLE IF EXISTS public.schema_migration;")
    return dsn


def test_migrate_on_empty_database_applies_all_migrations(clean_dsn: str) -> None:
    applied = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    expected_versions = [m.version for m in discover_migrations(REPO_MIGRATIONS_DIR)]
    assert [m.version for m in applied] == expected_versions


def test_remigration_is_a_no_op(clean_dsn: str) -> None:
    first = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)
    assert first  # la première passe a bien fait quelque chose

    second = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    assert second == []


def test_all_31_objects_are_present_and_commented(clean_dsn: str) -> None:
    import psycopg

    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    with psycopg.connect(clean_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, obj_description(c.oid, 'pg_class')
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('raw', 'core', 'mart')
               AND c.relkind IN ('r', 'm')
            """
        )
        rows = cur.fetchall()

    assert len(rows) == 31, f"attendu 31 tables/vues, trouvé {len(rows)}"
    uncommented = [name for name, comment in rows if not comment]
    assert not uncommented, f"objets sans commentaire : {uncommented}"


def test_refuses_to_start_when_an_applied_migration_is_modified(clean_dsn: str) -> None:
    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    # on ne modifie jamais un fichier du dépôt : on rejoue le scénario dans un répertoire
    # temporaire dont le contenu de la migration 001 a été altéré après coup.
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for path in REPO_MIGRATIONS_DIR.glob("*.sql"):
            shutil.copy(path, tmp_dir / path.name)
        tampered = tmp_dir / "001_schemas.sql"
        tampered.write_text(
            tampered.read_text(encoding="utf-8") + "\n-- altération post-application\n",
            encoding="utf-8",
        )

        with pytest.raises(MigrationDriftError):
            migrate(dsn=clean_dsn, migrations_dir=tmp_dir)

        # rien de nouveau n'a dû être appliqué : le refus est total, pas partiel
        report = status(dsn=clean_dsn, migrations_dir=tmp_dir)
        assert report.drift


def test_pg_dump_schema_only_is_stable_across_two_runs(clean_dsn: str) -> None:
    import shutil
    import subprocess

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        pytest.skip("pg_dump introuvable sur PATH.")

    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    def _dump() -> str:
        result = subprocess.run(  # noqa: S603
            [pg_dump, "--schema-only", "--no-owner", "--no-privileges", clean_dsn],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    first = _dump()
    second = _dump()

    assert first == second


def test_migrate_refuses_to_run_while_another_runner_holds_the_lock(clean_dsn: str) -> None:
    """Deux runners concurrents (deux CronJob, ou un déploiement pendant une migration
    manuelle) ne doivent pas appliquer la même migration en parallèle : le second échouerait
    au milieu du DDL, dans un état partiellement appliqué. Ici, une connexion tierce détient
    le verrou de session ; migrate() doit refuser immédiatement et ne rien appliquer.
    """
    import psycopg

    with psycopg.connect(clean_dsn) as holder:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        holder.commit()

        with pytest.raises(MigrationLockError):
            migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

        with holder.cursor() as cur:
            cur.execute("SELECT to_regclass('public.schema_migration');")
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None, "aucune migration ne doit avoir été amorcée sous verrou"

    # Verrou relâché avec la connexion : le runner repasse normalement.
    applied = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)
    assert len(applied) == len(discover_migrations(REPO_MIGRATIONS_DIR))


def test_lock_is_released_after_a_successful_migration(clean_dsn: str) -> None:
    """Le verrou est de portée session : s'il n'est pas relâché explicitement, une connexion
    poolée le garderait indéfiniment. Après une migration réussie, il doit être libre.
    """
    import psycopg

    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    with psycopg.connect(clean_dsn) as probe, probe.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True, "le verrou n'a pas été relâché après la migration"
        cur.execute("SELECT pg_advisory_unlock(%s);", (ADVISORY_LOCK_KEY,))
