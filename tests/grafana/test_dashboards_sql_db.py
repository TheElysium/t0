"""Run the session-detail dashboards' real rawSql against PostgreSQL, skipped without
T0_TEST_POSTGRES_DSN (same discipline as tests/mcp/test_db_reader.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

from t0.db.runner import migrate
from t0.ops.refresh import refresh_all_mart_views

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "deploy" / "helm" / "t0" / "dashboards"
TEST_DSN_ENV_VAR = "T0_TEST_POSTGRES_DSN"

TRI_ID = 1
SIMPLE_SWIM_ID = 20
LATE_LEG_ID = 6

Row = dict[str, object]

# activity_id, sport_code, is_parent, parent_activity_id, leg_order, distance_m, swim_time_s
ACTIVITIES: list[tuple[int, str, bool, int | None, int | None, int | None, int | None]] = [
    (TRI_ID, "multisport", True, None, None, None, None),
    (2, "open_water", False, TRI_ID, 1, 1500, None),
    (3, "transition", False, TRI_ID, 2, None, None),
    (4, "cycling", False, TRI_ID, 3, 40000, None),
    (5, "running", False, TRI_ID, 4, 10000, None),
    (SIMPLE_SWIM_ID, "lap_swimming", False, None, None, 200, 200),
]

# activity_id, lap_index, offset_s, distance_m, duration_s (lap_index not 0-based on legs)
LAPS: list[tuple[int, int, int, int | None, float]] = [
    (2, 0, 0, 1500, 1800),
    (3, 1, 1800, None, 120),
    (4, 2, 1920, 40000, 4000),
    (5, 5, 6000, 1000, 240),
    (5, 6, 6240, 1000, 236),
    (SIMPLE_SWIM_ID, 0, 0, 100, 100),
    (SIMPLE_SWIM_ID, 1, 100, None, 30),
    (SIMPLE_SWIM_ID, 2, 130, 100, 119.6),
]

# length_index, length_type, duration_s (all on SIMPLE_SWIM_ID)
SWIM_LENGTHS: list[tuple[int, str, float]] = [(0, "active", 50.0), (1, "idle", 30.0)]


def _require_test_dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} not set: no test PostgreSQL available.")
    return dsn


def _insert_activity(
    cur: psycopg.Cursor[tuple[object, ...]],
    spec: tuple[int, str, bool, int | None, int | None, int | None, int | None],
) -> None:
    activity_id, sport, is_parent, parent, leg, distance, swim_time = spec
    cur.execute(
        "INSERT INTO core.activity (activity_id, athlete_id, sport_code, start_time, "
        "start_time_local, local_date, tz_offset_s, duration_s, is_parent, "
        "parent_activity_id, leg_order, distance_m, swim_time_s) SELECT %s, athlete_id, %s, "
        "'2026-10-11T06:00:00Z', '2026-10-11T08:00:00', '2026-10-11', 7200, 3600, %s, %s, %s, "
        "%s, %s FROM core.athlete",
        (activity_id, sport, is_parent, parent, leg, distance, swim_time),
    )


def _insert_lap(
    cur: psycopg.Cursor[tuple[object, ...]],
    lap: tuple[int, int, int, int | None, float],
) -> None:
    activity_id, lap_index, offset_s, distance, duration = lap
    cur.execute(
        "INSERT INTO core.activity_lap (activity_id, lap_index, start_time, duration_s, "
        "distance_m, avg_hr, avg_power) VALUES (%s, %s, timestamptz '2026-10-11T06:00:00Z' "
        "+ %s * interval '1 second', %s, %s, 150, CASE WHEN %s = 4 THEN 200 END)",
        (activity_id, lap_index, offset_s, duration, distance, activity_id),
    )


@pytest.fixture
def seeded_dsn() -> str:
    dsn = _require_test_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for schema in ("raw", "core", "mart"):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        cur.execute("DROP TABLE IF EXISTS public.schema_migration;")
    migrate(dsn=dsn, migrations_dir=REPO_ROOT / "migrations")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO core.athlete (display_name) VALUES ('dashboard sql athlete')")
        for spec in ACTIVITIES:
            _insert_activity(cur, spec)
        for lap in LAPS:
            _insert_lap(cur, lap)
        for length_index, length_type, duration in SWIM_LENGTHS:
            cur.execute(
                "INSERT INTO core.swim_length (activity_id, length_index, lap_index, "
                "start_time, duration_s, length_type) VALUES (%s, %s, 0, "
                "'2026-10-11T06:00:00Z', %s, %s)",
                (SIMPLE_SWIM_ID, length_index, duration, length_type),
            )
        outcomes = refresh_all_mart_views(conn)
        assert all(o.succeeded for o in outcomes), outcomes
    return dsn


def _run_panel(dsn: str, dashboard: str, title: str, activity_id: int) -> list[Row]:
    board = json.loads((DASHBOARDS_DIR / f"session-detail-{dashboard}.json").read_text("utf-8"))
    (panel,) = [p for p in board["panels"] if p.get("title") == title]
    sql = panel["targets"][0]["rawSql"].replace("$activity_id", str(activity_id))
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)
        names = [d.name for d in cur.description or []]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _insert_late_leg(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        _insert_activity(cur, (LATE_LEG_ID, "running", False, TRI_ID, 5, 5000, None))
        _insert_lap(cur, (LATE_LEG_ID, 9, 7000, 1000, 300))


def test_tri_run_leg_pace_truncates_minutes(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "tri", "Laps", TRI_ID)

    run_paces = [r["Pace"] for r in rows if r["Leg"] == "Running"]
    assert run_paces == ["4:00", "3:56"]


def test_tri_swim_leg_pace_and_bike_pace_empty(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "tri", "Laps", TRI_ID)

    by_leg = {r["Leg"]: r["Pace"] for r in rows}
    assert by_leg["Open water"] == "2:00"
    assert by_leg["Cycling"] is None


def test_tri_laps_exclude_transitions_and_number_per_leg(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "tri", "Laps", TRI_ID)

    assert [(r["Leg"], r["Lap"]) for r in rows] == [
        ("Open water", 1),
        ("Cycling", 1),
        ("Running", 1),
        ("Running", 2),
    ]


def test_tri_leg_ingested_after_refresh_still_shows_laps(seeded_dsn: str) -> None:
    _insert_late_leg(seeded_dsn)

    rows = _run_panel(seeded_dsn, "tri", "Laps", TRI_ID)

    late = [r for r in rows if r["Leg"] == "Running"][-1]
    assert (late["Lap"], late["Pace"]) == (1, "5:00")
    assert len(rows) == 5


def test_swim_lap_paces_round_once_then_split(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "swim", "Laps", SIMPLE_SWIM_ID)

    assert [r["Pace (/100 m)"] for r in rows] == ["1:40", None, "2:00"]


def test_swim_stroke_metrics_average_pace(seeded_dsn: str) -> None:
    (row,) = _run_panel(seeded_dsn, "swim", "Stroke metrics", SIMPLE_SWIM_ID)

    assert row["Avg pace"] == "1:40"


def test_run_laps_start_is_offset_from_first_lap(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "run", "Laps", 5)

    assert [(r["Lap"], r["Start"], r["Duration"]) for r in rows] == [
        (1, "00:00:00", "0:04:00"),
        (2, "00:04:00", "0:03:56"),
    ]


def test_run_laps_pace_is_minutes_seconds(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "run", "Laps", 5)

    assert [r["Pace (min/km)"] for r in rows] == ["4:00", "3:56"]


def test_bike_laps_read_the_leg(seeded_dsn: str) -> None:
    (row,) = _run_panel(seeded_dsn, "bike", "Laps", 4)

    assert (row["Lap"], row["Avg power (W)"]) == (1, 200)


def test_swim_lengths_keep_idle_rows(seeded_dsn: str) -> None:
    rows = _run_panel(seeded_dsn, "swim", "Swim lengths", SIMPLE_SWIM_ID)

    assert [(r["Length"], r["Type"], r["Time (s)"]) for r in rows] == [
        (0, "active", 50.0),
        (1, "idle", 30.0),
    ]
