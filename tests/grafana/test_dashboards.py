"""Static tests for L9 batch: Grafana dashboards (deploy/helm/t0/dashboards/*.json).

No network, no Grafana, no real PostgreSQL. Three families of checks (L9 batch requirements):

1. Each JSON file is parsable with expected top-level keys (uid, title, panels, panel shape).
2. All raw SQL queries from dashboards (panels, annotations, templating variables) are
   extracted, then each table/view schema.object and column alias.column reference is
   validated against a column registry derived from migrations.
3. No dashboard uid is duplicated.

Column registry construction (documented choice):

- For core.* tables: AUTOMATIC extraction by regex on CREATE TABLE core.xxx blocks from
  migrations 003-006. Reliable because these blocks are flat DDL, one column per line.
- For mart.* materialized views: NO generic extraction (their final SELECT stacks CTEs
  and auto-flattening output aliases is fragile). Instead, EXPLICIT output column list
  manually copied from migrations/007_mart.sql CREATE MATERIALIZED VIEW definitions, as
  redefined by later migrations: 008 for mart.activity_load, 011 for mart.activity_wide
  (each DROPs and recreates, LATEST definition wins). test_expected_mart_columns_exist_in_migrations
  verifies this manual list has not drifted from actual view names in migrations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
DASHBOARDS_DIR = REPO_ROOT / "deploy" / "helm" / "t0" / "dashboards"

DASHBOARD_FILES = sorted(DASHBOARDS_DIR.glob("*.json")) if DASHBOARDS_DIR.exists() else []

# --- Column registry ---------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE\s+(?P<schema>\w+)\.(?P<table>\w+)\s*\((?P<body>.*?)\n\);",
    re.DOTALL,
)
# Column line: starts with identifier, then type. Exclude table constraints (PRIMARY KEY,
# UNIQUE, CHECK, FOREIGN KEY, CONSTRAINT) that don't declare new columns.
_COLUMN_LINE_RE = re.compile(r"^\s*(?P<col>[a-z_][a-z0-9_]*)\s+\S")
_CONSTRAINT_KEYWORDS = (
    "PRIMARY KEY",
    "UNIQUE",
    "CHECK",
    "FOREIGN KEY",
    "CONSTRAINT",
    "EXCLUDE",
)


def _parse_core_tables() -> dict[str, set[str]]:
    """Extract {"core.table": {columns...}} from migrations 003-006 CREATE TABLE."""
    tables: dict[str, set[str]] = {}
    for filename in (
        "003_core_ref.sql",
        "004_core_activity.sql",
        "005_core_series.sql",
        "006_core_wellness.sql",
        # Coaching batch: flat CREATE TABLE DDL like 003-006, same extraction applies
        # (dashboards query core.note / core.plan_week annotations).
        "016_coach_notes.sql",
        "017_plan.sql",
    ):
        text = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
        for match in _CREATE_TABLE_RE.finditer(text):
            full_name = f"{match.group('schema')}.{match.group('table')}"
            columns: set[str] = set()
            # Top-level split on commas (no column definitions contain unclosed nested
            # parens on same line except inline DEFAULT/CHECK, neutralized by constraint
            # keyword detection rather than full SQL parser).
            for raw_line in match.group("body").splitlines():
                line = raw_line.strip().rstrip(",")
                if not line:
                    continue
                upper = line.upper()
                if any(upper.startswith(kw) for kw in _CONSTRAINT_KEYWORDS):
                    continue
                col_match = _COLUMN_LINE_RE.match(line)
                if col_match:
                    columns.add(col_match.group("col"))
            tables[full_name] = columns
    return tables


CORE_TABLES = _parse_core_tables()

# Output columns of mart.* views, manually copied from migrations/007_mart.sql and
# migrations/008_mart_activity_load_fix.sql (008 is authoritative for mart.activity_load).
MART_VIEWS: dict[str, set[str]] = {
    "mart.pmc": {
        "athlete_id",
        "local_date",
        "tss",
        "ctl",
        "atl",
        "tsb",
        "tsb_same_day",
        "ctl_reliable",
    },
    "mart.daily_load": {
        "athlete_id",
        "local_date",
        "tss",
        "n_activities",
        "duration_s",
        "has_estimated_load",
    },
    "mart.activity_load": {
        "activity_id",
        "athlete_id",
        "sport_code",
        "discipline",
        "local_date",
        "threshold_id",
        "data_quality",
        "needs_manual_rpe",
        "intensity_factor",
        "intensity_factor_method",
        "hr_tss",
        "hr_seconds",
    },
    "mart.power_curve_best": {
        "kind",
        "duration_s",
        "value",
        "activity_id",
        "local_date",
    },
    "mart.hr_zone_time": {
        # NO athlete_id: view does not project it (used only in internal join on
        # core.threshold). Dashboard writing z.athlete_id must FAIL, not pass via overly
        # permissive registry.
        "activity_id",
        "local_date",
        "sport_code",
        "zone_code",
        "zone_order",
        "seconds",
    },
    "mart.activity_wide": {
        "activity_id",
        "local_date",
        "start_time_local",
        "sport_code",
        "sport",
        "name",
        "duration_s",
        # Added by 022: active swim time, the sTSS/IF denominator for swims.
        "swim_time_s",
        "distance_m",
        "avg_hr",
        "max_hr",
        "avg_power",
        "np_watts",
        "np_reliable",
        "ascent_m",
        "data_quality",
        "needs_manual_rpe",
        "tss",
        "tss_method",
        "tss_hr",
        "trimp",
        "intensity_factor",
        # Added by 011, counterpart to tss_method: GAP-derived IF interprets differently
        # from power-derived IF.
        "intensity_factor_method",
        "decoupling_pct",
        "efficiency_factor",
    },
    # Triathlon race view (migration 025): one row per multisport parent, one column
    # block per leg. Manually copied from its final SELECT projection, like the others.
    "mart.triathlon_session": {
        "activity_id",
        "local_date",
        "start_time_local",
        "name",
        "sport_code",
        "legs_count",
        "duration_s",
        "distance_m",
        "avg_hr",
        "max_hr",
        "tss",
        "trimp",
        "swim_activity_id",
        "swim_duration_s",
        "swim_distance_m",
        "swim_avg_hr",
        "t1_activity_id",
        "t1_duration_s",
        "bike_activity_id",
        "bike_duration_s",
        "bike_distance_m",
        "bike_avg_speed_mps",
        "bike_avg_power",
        "bike_tss",
        "t2_activity_id",
        "t2_duration_s",
        "run_activity_id",
        "run_duration_s",
        "run_distance_m",
        "run_avg_hr",
        "run_tss",
    },
    # Coaching views (migrations 018/019). Same rule as above: manually copied from
    # each view's FINAL SELECT projection.
    "mart.wellness_baseline": {
        "athlete_id",
        "local_date",
        "hrv_value",
        "hrv_baseline",
        "hrv_dev_ratio",
        "rhr_value",
        "rhr_baseline",
        "rhr_dev_bpm",
    },
    "mart.ramp_risk": {
        "athlete_id",
        "local_date",
        "tss",
        "trimp",
        "ctl",
        "atl",
        "tsb",
        "ctl_reliable",
        "dctl_per_week",
        "monotony",
        "strain",
        "estimated_share_7d",
    },
    "mart.coach_flags": {
        "athlete_id",
        "local_date",
        "rule_code",
        "level",
        "detail",
    },
    "mart.power_pace_zone_time": {
        "activity_id",
        "local_date",
        "sport_code",
        "zone_system",
        "zone_code",
        "seconds",
    },
}

KNOWN_TABLES: dict[str, set[str]] = {**CORE_TABLES, **MART_VIEWS}


# --- SQL query extraction from dashboards ------------------------------------------


def _iter_sql_strings(node: Any) -> list[str]:
    """Return all strings in rawSql/query/definition keys.

    Recursively descend entire JSON (panels, targets, annotations, templating.list):
    more robust than hard-coded paths if a panel adds nesting (e.g., row panels with
    nested panels).
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("rawSql", "query", "definition") and isinstance(value, str):
                # "query" also used by "custom" variables (static list, not SQL): keep
                # only strings that look like SQL.
                if key != "query" or re.search(r"\bSELECT\b", value, re.IGNORECASE):
                    found.append(value)
            else:
                found.extend(_iter_sql_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_sql_strings(item))
    return found


_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?P<schema>core|mart)\.(?P<table>\w+)(?:\s+(?:AS\s+)?(?P<alias>[a-zA-Z_]\w*))?",
    re.IGNORECASE,
)
# alias.column, excluding false positives "core.xxx"/"mart.xxx" already covered above
# (schema never acts as column alias in our dashboards).
_COLUMN_REF_RE = re.compile(r"\b(?P<alias>[a-zA-Z_]\w*)\.(?P<col>[a-zA-Z_]\w*)\b")
_SQL_KEYWORDS_AS_ALIAS = {"core", "mart"}
# PostgreSQL functions/pseudo-columns appearing as "alias.col" without being table columns
# (none here, kept for future extension).
_NON_COLUMN_SUFFIXES: set[str] = set()


def _substitute_grafana_macros(sql: str) -> str:
    """Replace non-SQL Grafana macros with parseable SQL equivalents.

    $__timeFilter(col) is not valid SQL: it is text substituted client-side by Grafana
    before sending to Postgres. Replace with trivial equivalent for users of real SQL
    parsers (pglast); regex alias/column extraction above needs no replacement (sees col
    directly in argument).
    """
    sql = re.sub(r"\$__timeFilter\(([^)]*)\)", r"(\1 >= now() AND \1 <= now())", sql)
    return re.sub(r"\$\w+", "'x'", sql)  # variables de templating ($athlete_id, $kind...)


def _check_query_against_schema(sql: str) -> list[str]:
    """Return list of errors (unknown table or column) found in sql."""
    errors: list[str] = []
    alias_to_table: dict[str, str] = {}
    for m in _TABLE_REF_RE.finditer(sql):
        full_name = f"{m.group('schema')}.{m.group('table')}"
        if full_name not in KNOWN_TABLES:
            errors.append(f"unknown table/view referenced: {full_name}")
            continue
        alias = m.group("alias") or m.group("table")
        alias_to_table[alias] = full_name

    for m in _COLUMN_REF_RE.finditer(sql):
        alias = m.group("alias")
        col = m.group("col")
        if alias in _SQL_KEYWORDS_AS_ALIAS:
            continue  # already covered by _TABLE_REF_RE (schema, not alias)
        if alias not in alias_to_table:
            continue  # unknown table alias in this query (noise)
        table = alias_to_table[alias]
        if col in _NON_COLUMN_SUFFIXES:
            continue
        if col not in KNOWN_TABLES[table]:
            errors.append(f"unknown column {alias}.{col} (table {table})")
    return errors


# --- Tests: file structure ---------------------------------------------------------


def test_dashboards_directory_contains_files() -> None:
    assert DASHBOARD_FILES, f"no .json files found in {DASHBOARDS_DIR}"


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_each_dashboard_is_valid_json_with_expected_keys(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(data.get("uid"), str), f"{path.name}: uid must be a string"
    assert data["uid"], f"{path.name}: missing uid"
    assert isinstance(data.get("title"), str), f"{path.name}: title must be a string"
    assert data["title"], f"{path.name}: missing title"
    assert isinstance(data.get("panels"), list), f"{path.name}: panels must be a list"
    assert data["panels"], f"{path.name}: panels must be non-empty list"

    for panel in data["panels"]:
        assert "id" in panel, f"{path.name}: panel missing id"
        assert "type" in panel, f"{path.name}: panel {panel.get('id')} missing type"
        if panel["type"] == "text":
            continue  # text/markdown panel: gridPos/targets not required
        assert "gridPos" in panel, f"{path.name}: panel {panel.get('id')} missing gridPos"
        assert panel.get("targets"), f"{path.name}: panel {panel.get('id')} missing targets"
        for target in panel["targets"]:
            assert target.get(
                "rawSql"
            ), f"{path.name}: panel {panel.get('id')} target missing rawSql"
            assert target.get("format") in (
                "time_series",
                "table",
            ), f"{path.name}: panel {panel.get('id')} unexpected format"


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_each_dashboard_query_references_only_known_objects(
    path: Path,
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    sql_strings = _iter_sql_strings(data)
    assert sql_strings, f"{path.name}: no SQL queries found (unexpected)"

    all_errors: list[str] = []
    all_errors.extend(
        f"{err}\n  query: {sql}" for sql in sql_strings for err in _check_query_against_schema(sql)
    )

    assert not all_errors, f"{path.name}:\n" + "\n".join(all_errors)


def test_no_dashboard_uid_is_duplicated() -> None:
    uids = [json.loads(p.read_text(encoding="utf-8"))["uid"] for p in DASHBOARD_FILES]
    assert len(uids) == len(set(uids)), f"duplicate uids: {uids}"


def _final_projection(view_name: str, migrations_text: str) -> str:
    """Return the PROJECTION LIST of final SELECT of view view_name.

    Neither regex nor pglast can extract columns from CTE-stacked views without catalog.
    But no need to solve everything: isolate the final SELECT (in our migrations, only one
    with keyword at column 0; CTE ones are always indented) and look at identifiers it
    projects. Output columns always appear, either by name or AS alias.
    """
    bloc_re = re.compile(
        rf"CREATE MATERIALIZED VIEW\s+{re.escape(view_name)}\s+AS(?P<body>.*?);\s*$",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    match = bloc_re.search(migrations_text)
    assert match, f"no definition found for {view_name}"
    body = match.group("body")

    starts = [m.start() for m in re.finditer(r"^SELECT", body, re.MULTILINE)]
    assert starts, f"{view_name}: no SELECT at column 0 (unexpected formatting)"
    final = body[starts[-1] :]

    fin = re.search(r"^\s*FROM", final, re.MULTILINE)
    return final[: fin.start()] if fin else final


def test_expected_mart_columns_exist_in_migrations() -> None:
    """Validate MART_VIEWS (manually copied) against actual view definitions.

    Checking that view NAMES exist is insufficient: registry initially gave athlete_id
    to mart.hr_zone_time, which does not project it. Phantom columns in registry worse
    than no registry—they ALLOW the broken query this file is meant to prevent, and
    panel fails to display for athlete months later.

    Inverse (real columns absent from MART_VIEWS) unchecked: harmless, test fails on
    first use.
    """
    # Scan ALL migrations in version order, not just 007/008: views redefined later
    # (011 for mart.activity_wide) read from LATEST definition. Hard-coding originals
    # would validate against stale view, silencing the guard it exists to catch.
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    }

    definitions: dict[str, str] = {}
    for _, text in sorted(sources.items()):
        for view_name in re.findall(
            r"CREATE (?:OR REPLACE )?MATERIALIZED VIEW\s+(mart\.\w+)", text, re.IGNORECASE
        ):
            definitions[view_name] = text

    for view_name, colonnes in MART_VIEWS.items():
        assert view_name in definitions, (
            f"{view_name} listed in MART_VIEWS but no matching CREATE MATERIALIZED VIEW "
            "in migrations: manual list has drifted"
        )
        # Keep most recent CREATE, not bare mention: migrations cite other views in
        # comments, relying on them would search wrong file.
        source = definitions[view_name]
        projection = _final_projection(view_name, source)
        identifiants = set(re.findall(r"[a-z_][a-z0-9_]*", projection))
        manquantes = sorted(c for c in colonnes if c not in identifiants)
        assert not manquantes, (
            f"{view_name}: {manquantes} in MART_VIEWS but missing from final projection. "
            f"Projection read: {projection}"
        )


def test_mart_column_guard_detects_phantom_column() -> None:
    """Counterproof: without it, we wouldn't know if guard above works.

    Replay its logic on deliberately fake column. If final projection extraction breaks
    (regex too permissive, wrong SELECT chosen), it accepts anything and guard becomes
    ornamental.
    """
    text_007 = (MIGRATIONS_DIR / "007_mart.sql").read_text(encoding="utf-8")
    projection = _final_projection("mart.hr_zone_time", text_007)
    identifiants = set(re.findall(r"[a-z_][a-z0-9_]*", projection))

    assert "zone_code" in identifiants  # actually projected
    assert "athlete_id" not in identifiants  # used in join, NEVER projected
    assert "completely_fake_column" not in identifiants


def test_core_tables_reference_is_not_empty() -> None:
    """Safeguard: if DDL regex breaks silently (0 tables extracted), column verification
    test would detect nothing—make it explicit."""
    assert len(CORE_TABLES) >= 15, f"only {len(CORE_TABLES)} core.* tables extracted"


def test_grafana_macros_are_substituted_before_strict_sql_analysis() -> None:
    """Document and verify $__timeFilter substitution before pglast use.

    $__timeFilter(col) is not standard SQL: it is Grafana text macro.
    This test locks behavior of _substitute_grafana_macros, used by anyone later
    validating query syntax with pglast (import pglast; pglast.parse_sql(...))
    without failing on these macros.
    """
    raw = (
        "SELECT 1 FROM mart.pmc p "
        "WHERE $__timeFilter(p.local_date) AND p.athlete_id = $athlete_id"
    )
    substituted = _substitute_grafana_macros(raw)

    assert "$__timeFilter" not in substituted
    assert "$athlete_id" not in substituted

    # pglast NOT a .venv dependency (batch prohibits new packages): available only via
    # global python3.11 mentioned in brief. Import best-effort and skip if absent rather
    # than fail—substitution itself already verified above without pglast.
    pglast = pytest.importorskip("pglast", reason="pglast available only outside .venv")
    pglast.parse_sql(substituted)  # must not raise
