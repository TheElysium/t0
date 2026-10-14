"""Reader integration tests against real PostgreSQL, skipped without
T0_TEST_POSTGRES_DSN (same discipline as tests/db/test_runner_integration.py)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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


def test_get_pmc_on_empty_marts_returns_an_empty_page(reader: Reader) -> None:
    assert handlers.get_pmc(reader, max_rows=500) == {"rows": [], "truncated": False}


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
    """Guard: the migration directory still parses and stays ordered."""
    versions = [m.version for m in discover_migrations(REPO_MIGRATIONS_DIR)]
    assert versions == sorted(versions)


PARENT_ID = 9_000_001
LEG_ID = 9_000_002


def _insert_activity(
    cur: Any, activity_id: int, *, is_parent: bool, parent: int | None, leg: int | None
) -> None:
    cur.execute(
        "INSERT INTO core.activity (activity_id, athlete_id, sport_code, start_time, "
        "start_time_local, local_date, tz_offset_s, duration_s, is_parent, "
        "parent_activity_id, leg_order) "
        "SELECT %s, a.athlete_id, 'lap_swimming', '2026-09-01T06:00:00Z', "
        "'2026-09-01T08:00:00', '2026-09-01', 7200, 600, %s, %s, %s "
        "FROM core.athlete a ORDER BY a.athlete_id LIMIT 1",
        (activity_id, is_parent, parent, leg),
    )


def _insert_parent_with_leg(dsn: str) -> None:
    import psycopg

    from t0.ops.refresh import refresh_all_mart_views

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO core.athlete (display_name) VALUES ('MCP laps athlete')")
        _insert_activity(cur, PARENT_ID, is_parent=True, parent=None, leg=None)
        _insert_activity(cur, LEG_ID, is_parent=False, parent=PARENT_ID, leg=1)
        for activity_id in (PARENT_ID, LEG_ID):
            cur.execute(
                "INSERT INTO core.activity_lap (activity_id, lap_index, start_time, "
                "duration_s, avg_hr, extra) VALUES (%s, 3, '2026-09-01T06:00:00Z', 300, "
                "150, '{\"start_position_lat\": 1}')",
                (activity_id,),
            )
            cur.execute(
                "INSERT INTO core.swim_length (activity_id, length_index, lap_index, "
                "start_time, duration_s, length_type) "
                "VALUES (%s, 0, 3, '2026-09-01T06:00:00Z', 31.5, 'idle')",
                (activity_id,),
            )
        outcomes = refresh_all_mart_views(conn)
        assert all(o.succeeded for o in outcomes), outcomes


def test_get_laps_on_a_leg_returns_laps_and_lengths(reader: Reader, migrated_dsn: str) -> None:
    _insert_parent_with_leg(migrated_dsn)

    result = handlers.get_laps(reader, activity_id=LEG_ID, max_rows=500)

    assert result is not None
    (lap,) = result["rows"]
    assert (lap["lap_index"], lap["avg_hr"]) == (3, 150)
    assert (lap["parent_activity_id"], lap["leg_order"]) == (PARENT_ID, 1)
    (length,) = result["lengths"]
    assert (length["length_type"], length["duration_s"]) == ("idle", 31.5)
    assert result["truncated"] is False
    assert result["lengths_truncated"] is False


def test_get_laps_on_a_parent_returns_none(reader: Reader, migrated_dsn: str) -> None:
    _insert_parent_with_leg(migrated_dsn)

    assert handlers.get_laps(reader, activity_id=LEG_ID, max_rows=500) is not None
    assert handlers.get_laps(reader, activity_id=PARENT_ID, max_rows=500) is None


def test_lap_views_exclude_parent_rows(reader: Reader, migrated_dsn: str) -> None:
    _insert_parent_with_leg(migrated_dsn)

    laps = reader.fetch("SELECT activity_id FROM mart.activity_lap", {})
    lengths = reader.fetch("SELECT activity_id FROM mart.swim_length", {})

    assert [r["activity_id"] for r in laps] == [LEG_ID]
    assert [r["activity_id"] for r in lengths] == [LEG_ID]


def test_mcp_role_can_select_the_lap_views(migrated_dsn: str) -> None:
    import psycopg

    with psycopg.connect(migrated_dsn, autocommit=True) as conn, conn.cursor() as cur:
        for view in ("mart.activity_lap", "mart.swim_length"):
            cur.execute("SELECT has_table_privilege('t0_mcp_ro', %s, 'SELECT')", (view,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] is True, view


def test_extra_is_not_a_column_of_the_lap_views(reader: Reader) -> None:
    rows = reader.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'mart' AND table_name IN ('activity_lap', 'swim_length')",
        {},
    )

    assert {r["table_name"] for r in rows} == {"activity_lap", "swim_length"}
    assert "extra" not in {r["column_name"] for r in rows}
