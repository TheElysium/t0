"""Reader integration tests against real PostgreSQL, skipped without
T0_TEST_POSTGRES_DSN (same discipline as tests/db/test_runner_integration.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from t0.db.runner import discover_migrations, migrate
from t0.mcp import handlers
from t0.mcp.db import Reader

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
TEST_DSN_ENV_VAR = "T0_TEST_POSTGRES_DSN"


def _require_test_dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} not set: no test PostgreSQL available.")
    return dsn


@pytest.fixture
def migrated_dsn() -> str:
    """DSN to a test database, reinitialized and migrated up to head."""
    import psycopg

    dsn = _require_test_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS raw CASCADE;")
        cur.execute("DROP SCHEMA IF EXISTS core CASCADE;")
        cur.execute("DROP SCHEMA IF EXISTS mart CASCADE;")
        cur.execute("DROP TABLE IF EXISTS public.schema_migration;")
    migrate(dsn=dsn, migrations_dir=REPO_MIGRATIONS_DIR)
    return dsn


@pytest.fixture
def reader(migrated_dsn: str) -> Reader:
    return Reader(dsn=migrated_dsn, statement_timeout_ms=2_000)


def _insert_bike_threshold(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.athlete (display_name) VALUES ('MCP test athlete') "
            "RETURNING athlete_id"
        )
        cur.fetchone()
        cur.execute(
            "INSERT INTO core.threshold (athlete_id, discipline, validity, "
            "ftp_watts, lthr_bpm) SELECT athlete_id, 'bike', '[2026-01-01, infinity)', "
            "245.0, 152 FROM core.athlete ORDER BY athlete_id DESC LIMIT 1"
        )
        cur.fetchone()


def test_readiness_check_succeeds(reader: Reader) -> None:
    reader.check()


def test_connection_options_reach_the_server(reader: Reader) -> None:
    rows = reader.fetch(
        "SELECT current_setting('statement_timeout') AS timeout, "
        "current_setting('transaction_read_only') AS read_only",
        {},
    )

    assert rows[0]["timeout"] == "2s"
    assert rows[0]["read_only"] == "on"


def test_read_only_connection_refuses_writes(reader: Reader) -> None:
    from t0.mcp.db import QueryError

    assert reader.fetch("SELECT 1", {}) == [{"?column?": 1}]

    with pytest.raises(QueryError):
        reader.fetch("CREATE TABLE public.mcp_should_not_write (id int)", {})


def test_get_pmc_on_empty_marts_returns_empty_list(reader: Reader) -> None:
    assert handlers.get_pmc(reader, max_rows=500) == []


def test_get_thresholds_round_trips_a_real_row(reader: Reader, migrated_dsn: str) -> None:
    _insert_bike_threshold(migrated_dsn)

    rows = handlers.get_thresholds(reader, max_rows=500, discipline="bike")

    assert len(rows) == 1
    assert rows[0]["ftp_watts"] == 245.0
    assert rows[0]["valid_to"] == "infinity"
    assert rows[0]["css_estimated"] is False


def test_statement_timeout_cuts_a_runaway_query(reader: Reader) -> None:
    from t0.mcp.db import QueryError

    with pytest.raises(QueryError):
        reader.fetch("SELECT pg_sleep(5)", {})


def test_discover_migrations_are_not_broken_by_this_package() -> None:
    """Guard: the MCP adds no migration and the directory still parses."""
    versions = [m.version for m in discover_migrations(REPO_MIGRATIONS_DIR)]
    assert versions == sorted(versions)
