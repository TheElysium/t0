"""Static test for migration 015 (core.sport_fit_map): no database.

Parses the seed INSERT of migrations/015_core_sport_fit_map.sql and checks it is
EXACTLY equal (no extra, no missing) to SPORT_CODE_BY_FIT_SPORT, the mapping actually
executed at ingestion (src/t0/transform/manual.py). Also checks every seeded
sport_code exists among the sport codes seeded by migrations/003_core_ref.sql -- the
foreign key, checked statically since there is no live PostgreSQL here.
"""

from __future__ import annotations

import re
from pathlib import Path

from t0.transform.manual import SPORT_CODE_BY_FIT_SPORT

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
SPORT_FIT_MAP_SQL = REPO_MIGRATIONS_DIR / "015_core_sport_fit_map.sql"
CORE_REF_SQL = REPO_MIGRATIONS_DIR / "003_core_ref.sql"


def _parse_sport_fit_map_seed(sql: str) -> dict[tuple[str, str | None], str]:
    """Line-based parse of the `INSERT INTO core.sport_fit_map (...) VALUES (...)` seed.

    Deliberately not a single clever regex: each seeded row is one parenthesized tuple
    of three comma-separated values on its own line, `'text'` or `NULL` for the first
    two, `'text'` for the third. Parsed line by line for readability and to keep failure
    messages pointing at a specific row.
    """
    match = re.search(
        r"INSERT INTO core\.sport_fit_map \([^)]*\) VALUES\s*(.*?);",
        sql,
        flags=re.S,
    )
    assert match is not None, "no INSERT INTO core.sport_fit_map found in 015"
    values_block = match.group(1)

    row_pattern = re.compile(r"\(\s*'([^']*)'\s*,\s*(NULL|'[^']*')\s*,\s*'([^']*)'\s*\)")
    rows = row_pattern.findall(values_block)
    assert rows, "no seeded row parsed out of core.sport_fit_map INSERT"

    seed: dict[tuple[str, str | None], str] = {}
    for fit_sport, fit_sub_sport_raw, sport_code in rows:
        fit_sub_sport = None if fit_sub_sport_raw == "NULL" else fit_sub_sport_raw.strip("'")
        seed[(fit_sport, fit_sub_sport)] = sport_code
    return seed


def _parse_sport_codes(sql: str) -> set[str]:
    """Line-based parse of the sport_code values seeded by `INSERT INTO core.sport`."""
    match = re.search(
        r"INSERT INTO core\.sport \([^)]*\) VALUES\s*(.*?)\s*ON CONFLICT",
        sql,
        flags=re.S,
    )
    assert match is not None, "no INSERT INTO core.sport found in 003"
    values_block = match.group(1)

    codes = re.findall(r"\(\s*'([^']*)'", values_block)
    assert codes, "no sport_code parsed out of core.sport INSERT"
    return set(codes)


def test_sport_fit_map_seed_matches_python_mapping_exactly() -> None:
    sql = SPORT_FIT_MAP_SQL.read_text(encoding="utf-8")
    seeded = _parse_sport_fit_map_seed(sql)

    missing_from_sql = set(SPORT_CODE_BY_FIT_SPORT) - set(seeded)
    extra_in_sql = set(seeded) - set(SPORT_CODE_BY_FIT_SPORT)
    assert not missing_from_sql, (
        f"pairs present in SPORT_CODE_BY_FIT_SPORT but missing from 015 seed: "
        f"{missing_from_sql}"
    )
    assert (
        not extra_in_sql
    ), f"pairs seeded in 015 but absent from SPORT_CODE_BY_FIT_SPORT: {extra_in_sql}"
    assert seeded == SPORT_CODE_BY_FIT_SPORT


def test_sport_fit_map_seeded_sport_codes_exist_in_core_sport() -> None:
    sport_fit_map_sql = SPORT_FIT_MAP_SQL.read_text(encoding="utf-8")
    core_ref_sql = CORE_REF_SQL.read_text(encoding="utf-8")

    seeded_sport_codes = set(_parse_sport_fit_map_seed(sport_fit_map_sql).values())
    known_sport_codes = _parse_sport_codes(core_ref_sql)

    unknown = seeded_sport_codes - known_sport_codes
    assert not unknown, (
        f"core.sport_fit_map (015) references sport_code(s) never seeded by "
        f"core.sport (003): {unknown}"
    )
