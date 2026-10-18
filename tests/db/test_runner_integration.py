"""Integration tests for migration runner against real PostgreSQL.

These tests run only if PostgreSQL is reachable via T0_TEST_POSTGRES_DSN
(a test-dedicated database, never production—each test cleans it via
DROP SCHEMA ... CASCADE before repopulating). Without this variable, tests skip rather
than fail: at L1 batch time, no local PostgreSQL or Docker container was available in
execution environment (see L1 end report)—this file is ready to run once a test database
exists (CI or dev machine).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from t0.db.runner import (
    ADVISORY_LOCK_KEY,
    MigrationDriftError,
    MigrationLockError,
    discover_migrations,
    migrate,
    status,
)

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
TEST_DSN_ENV_VAR = "T0_TEST_POSTGRES_DSN"


def _require_test_dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV_VAR} not set: no test PostgreSQL available " "(see L1 end report)."
        )
    return dsn


@pytest.fixture
def clean_dsn() -> str:
    """DSN to test database, reinitialized (app schemas + tracking table)."""
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
    assert first  # first pass did apply something

    second = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    assert second == []


def test_all_42_objects_are_present_and_commented(clean_dsn: str) -> None:
    import psycopg

    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    with psycopg.connect(clean_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, obj_description(c.oid, 'pg_class')
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('raw', 'core', 'mart')
               AND c.relkind IN ('r', 'm', 'v')
            """
        )
        rows = cur.fetchall()

    assert len(rows) == 42, f"expected 42 tables/views, found {len(rows)}"
    uncommented = [name for name, comment in rows if not comment]
    assert not uncommented, f"objects without comment: {uncommented}"


def test_refuses_to_start_when_an_applied_migration_is_modified(clean_dsn: str) -> None:
    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    # never modify repo file: replay scenario in temp dir with migration 001 content
    # altered post-application.
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for path in REPO_MIGRATIONS_DIR.glob("*.sql"):
            shutil.copy(path, tmp_dir / path.name)
        tampered = tmp_dir / "001_schemas.sql"
        tampered.write_text(
            tampered.read_text(encoding="utf-8") + "\n-- post-application alteration\n",
            encoding="utf-8",
        )

        with pytest.raises(MigrationDriftError):
            migrate(dsn=clean_dsn, migrations_dir=tmp_dir)

        # nothing new should apply: refusal is total, not partial
        report = status(dsn=clean_dsn, migrations_dir=tmp_dir)
        assert report.drift


def test_pg_dump_schema_only_is_stable_across_two_runs(clean_dsn: str) -> None:
    import shutil
    import subprocess

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        pytest.skip("pg_dump not found on PATH.")

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
    """Two concurrent runners (two CronJobs, or deploy during manual migration) must not
    apply same migration in parallel: second would fail mid-DDL in partially applied state.
    Here, third connection holds session lock; migrate() must refuse immediately, apply
    nothing.
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
        assert row[0] is None, "no migration should have started under lock"

    # Lock released with connection: runner proceeds normally.
    applied = migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)
    assert len(applied) == len(discover_migrations(REPO_MIGRATIONS_DIR))


def test_lock_is_released_after_a_successful_migration(clean_dsn: str) -> None:
    """Lock is session-scoped: unreleased, pooled connection holds it forever. After
    successful migration, must be free.
    """
    import psycopg

    migrate(dsn=clean_dsn, migrations_dir=REPO_MIGRATIONS_DIR)

    with psycopg.connect(clean_dsn) as probe, probe.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True, "lock not released after migration"
        cur.execute("SELECT pg_advisory_unlock(%s);", (ADVISORY_LOCK_KEY,))
