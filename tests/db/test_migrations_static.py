"""Static tests for L1 batch: require no PostgreSQL, no network.

Two families of checks:

1. Pure runner properties (:mod:`t0.db.runner`): migration discovery, checksum,
   drift detection, planning — tested on disposable ``.sql`` files in ``tmp_path``.
2. Regressions on actual ``migrations/`` content in the repository: naming, no version
   duplicates, presence of ``ON CONFLICT DO NOTHING`` on reference-data seeds (explicit
   batch requirement, absent from frozen DDL itself).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from t0.db.runner import (
    ADVISORY_LOCK_KEY,
    MIGRATIONS_DIR_ENV_VAR,
    AppliedMigration,
    MigrationError,
    MigrationLockError,
    acquire_lock,
    compute_checksum,
    detect_drift,
    discover_migrations,
    plan_pending,
    release_lock,
    resolve_migrations_dir,
)

if TYPE_CHECKING:
    from datetime import datetime

    from t0.db.runner import Migration

REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
THRESHOLDS_FIXTURE = FIXTURES_DIR / "thresholds_example.sql"


def _write(tmp_path: Path, filename: str, content: str = "SELECT 1;") -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


# --- Pure properties ---


def test_discover_migrations_orders_by_numeric_version(tmp_path: Path) -> None:
    _write(tmp_path, "002_second.sql")
    _write(tmp_path, "010_tenth.sql")
    _write(tmp_path, "001_first.sql")

    migrations = discover_migrations(tmp_path)

    assert [m.version for m in migrations] == [1, 2, 10]
    assert [m.name for m in migrations] == ["first", "second", "tenth"]


def test_discover_migrations_ignores_files_not_matching_the_pattern(tmp_path: Path) -> None:
    _write(tmp_path, "001_schemas.sql")
    _write(tmp_path, "README.sql")  # no numeric prefix
    _write(tmp_path, "notes.txt")

    migrations = discover_migrations(tmp_path)

    assert [m.name for m in migrations] == ["schemas"]


def test_discover_migrations_rejects_duplicate_version(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    _write(tmp_path, "001_b.sql")

    with pytest.raises(MigrationError, match="present twice"):
        discover_migrations(tmp_path)


def test_checksum_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    path_a = _write(tmp_path, "001_a.sql", "SELECT 1;")
    path_b = _write(tmp_path, "002_b.sql", "SELECT 1;")
    path_c = _write(tmp_path, "003_c.sql", "SELECT 2;")

    assert compute_checksum(path_a) == compute_checksum(path_b)
    assert compute_checksum(path_a) != compute_checksum(path_c)
    # stable across calls
    assert compute_checksum(path_a) == compute_checksum(path_a)


def test_detect_drift_is_empty_when_everything_matches(tmp_path: Path) -> None:
    path = _write(tmp_path, "001_schemas.sql")
    migrations = discover_migrations(tmp_path)
    applied = [
        AppliedMigration(
            version=1,
            name="schemas",
            checksum=compute_checksum(path),
            applied_at=_now(),
        )
    ]

    assert detect_drift(migrations, applied) == []


def test_detect_drift_flags_a_modified_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "001_schemas.sql", "SELECT 1;")
    original_checksum = compute_checksum(path)
    # an applied migration should never be modified — simulate the incident
    path.write_text("SELECT 2;", encoding="utf-8")
    migrations = discover_migrations(tmp_path)
    applied = [
        AppliedMigration(version=1, name="schemas", checksum=original_checksum, applied_at=_now())
    ]

    issues = detect_drift(migrations, applied)

    assert len(issues) == 1
    assert "1" in issues[0]
    assert "checksum changed" in issues[0]


def test_detect_drift_flags_a_deleted_file(tmp_path: Path) -> None:
    migrations: list[Migration] = []  # nothing on disk
    applied = [AppliedMigration(version=1, name="schemas", checksum="deadbeef", applied_at=_now())]

    issues = detect_drift(migrations, applied)

    assert len(issues) == 1
    assert "vanished" in issues[0]


def test_plan_pending_excludes_already_applied_versions(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    _write(tmp_path, "002_b.sql")
    _write(tmp_path, "003_c.sql")
    migrations = discover_migrations(tmp_path)

    pending = plan_pending(migrations, applied_versions={1})

    assert [m.version for m in pending] == [2, 3]


def test_plan_pending_is_empty_when_everything_is_applied(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql")
    migrations = discover_migrations(tmp_path)

    pending = plan_pending(migrations, applied_versions={1})

    assert pending == []


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --- Regressions on actual migrations/ content ----------------------------


EXPECTED_MIGRATION_FILES = [
    "001_schemas.sql",
    "002_raw.sql",
    "003_core_ref.sql",
    "004_core_activity.sql",
    "005_core_series.sql",
    "006_core_wellness.sql",
    "007_mart.sql",
    "008_mart_activity_load_fix.sql",
    "009_scheduler.sql",
    "010_raw_fit_file_sha_comment.sql",
    "011_intensity_factor_arbitration.sql",
    "012_raw_fit_file_manual_upload.sql",
    "013_threshold_power_source_home_trainer.sql",
    "014_core_activity_manual_source.sql",
    "015_core_sport_fit_map.sql",
    "016_coach_notes.sql",
    "017_plan.sql",
    "018_baselines.sql",
    "019_signals.sql",
    "020_mcp_ro_grants.sql",
    "021_mcp_core_grants.sql",
    "022_mart_activity_wide_swim_time.sql",
    "023_core_activity_leg_source.sql",
    "024_shrink_synthetic_id_range.sql",
    "025_mart_triathlon_session.sql",
]


def test_repo_migrations_directory_matches_the_expected_split() -> None:
    """Lock down the EXACT list of repository migrations and their contiguity from 1.

    Deliberately no commented migration enumeration: a docstring listing them becomes
    stale with each addition, and a stale docstring misleads more than it informs. Each
    file carries its own WHY in its header, which cannot diverge.
    """
    migrations = discover_migrations(REPO_MIGRATIONS_DIR)

    assert [m.path.name for m in migrations] == EXPECTED_MIGRATION_FILES
    assert [m.version for m in migrations] == list(range(1, len(EXPECTED_MIGRATION_FILES) + 1))


def test_repo_migrations_have_no_checksum_collision() -> None:
    migrations = discover_migrations(REPO_MIGRATIONS_DIR)
    checksums = [m.checksum for m in migrations]

    assert len(checksums) == len(set(checksums))


@pytest.mark.parametrize(
    "filename",
    ["003_core_ref.sql", "006_core_wellness.sql"],
)
def test_seed_inserts_are_idempotent(filename: str) -> None:
    """Explicit batch requirement: seeds must be reapplicable without effect.

    The frozen DDL itself (research/05-postgres-schema.md §3) lacks this clause on its
    seed INSERT — this is a deliberate and documented batch addition, verified here by
    regression: each INSERT INTO in a seed file must be followed (before final semicolon)
    by ON CONFLICT DO NOTHING.
    """
    content = (REPO_MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    insert_blocks = re.findall(r"INSERT INTO.*?;", content, flags=re.S)

    assert insert_blocks, f"no INSERT found in {filename}"
    for block in insert_blocks:
        assert (
            "ON CONFLICT DO NOTHING" in block
        ), f"non-idempotent seed in {filename}:\n{block[:120]}..."


def test_all_31_tables_and_views_are_commented_in_the_concatenated_migrations() -> None:
    """COMMENT ON coverage check, without depending on pglast (not a batch runtime
    dependency): textual count of CREATE TABLE / CREATE MATERIALIZED VIEW on one side,
    COMMENT ON TABLE / COMMENT ON MATERIALIZED VIEW on the other.

    31 objects from frozen DDL (research/05-postgres-schema.md) + 1 (raw.poll_window, L6
    batch, migration 009) + 1 (core.sport_fit_map, L1 batch, migration 015)
    + 6 (coaching batch, migrations 016-019: core.note, core.plan_week, and the
    materialized views wellness_baseline, power_pace_zone_time, ramp_risk, coach_flags)
    + 1 (mart.triathlon_session, migration 025)
    = 40.
    """
    full_text = "\n".join(
        (REPO_MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        for name in EXPECTED_MIGRATION_FILES
    )
    created = re.findall(r"CREATE (?:TABLE|MATERIALIZED VIEW)\s+([a-z_]+\.[a-z_]+)", full_text)
    commented = re.findall(
        r"COMMENT ON (?:TABLE|MATERIALIZED VIEW)\s+([a-z_]+\.[a-z_]+)", full_text
    )

    assert len(set(created)) == 40, f"expected 40 objects, found {len(set(created))}: {created}"
    missing = set(created) - set(commented)
    assert not missing, f"created objects without matching COMMENT ON: {missing}"


# --- Thresholds fixture is not a migration ---


def test_thresholds_fixture_is_never_discovered_as_a_migration(tmp_path: Path) -> None:
    """Non-negotiable: the thresholds fixture must NEVER be applied automatically.

    Three angles, because "name doesn't match pattern" verifies by execution, not by
    reading:
    1. the repository's migrations/ directory contains only expected migrations;
    2. scanning the fixtures directory discovers nothing;
    3. even *copied into* a migrations directory, the fixture stays ignored — filename
       decides, not location.
    """
    assert THRESHOLDS_FIXTURE.is_file()
    assert [m.path.name for m in discover_migrations(REPO_MIGRATIONS_DIR)] == (
        EXPECTED_MIGRATION_FILES
    )
    assert discover_migrations(FIXTURES_DIR) == []

    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / THRESHOLDS_FIXTURE.name).write_text(
        THRESHOLDS_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert [m.path.name for m in discover_migrations(tmp_path)] == ["001_first.sql"]


def test_thresholds_fixture_leaves_rftp_null_and_matches_the_communicated_values() -> None:
    """Regression on threshold values: a wrong threshold raises no error.

    It produces plausible and wrong numbers across training history. Values come from
    addendum §5 (communicated by athlete 2026-08-19, LTHR 2026-08-21); rftp_watts must
    stay NULL — neither derived from bike FTP nor estimated from threshold pace.

    The validity lower bound is **2026-05-19**: three-month backdating confirmed by
    athlete 2026-08-21. This is an assumed hypothesis, not a measurement; changing it
    silently shifts the start of all training load, hence this guard.
    """
    content = THRESHOLDS_FIXTURE.read_text(encoding="utf-8")
    statements = re.findall(r"INSERT INTO.*?;", content, flags=re.S)
    by_discipline = {
        d: next(b for b in statements if f"'{d}'," in b) for d in ("bike", "run", "swim", "all")
    }

    assert "220, 190, 55, 70.0," in by_discipline["bike"]
    # 4:30 min/km = 270 s/km; rFTP explicitly NULL in value list.
    assert "270, NULL, 190, 55, 70.0," in by_discipline["run"]
    # 1:50 min/100 m = 0.909091 m/s, measured (css_estimated = false).
    assert "0.909091, false, 190, 55, 70.0," in by_discipline["swim"]
    # LTHR 170 bpm, carried by cross-discipline row: condition for hrTSS existence.
    assert "170, 190, 55, 70.0," in by_discipline["all"]
    for block in by_discipline.values():
        assert "daterange('2026-05-19', NULL, '[)')" in block
    assert not re.search(
        r"rftp_watts\s*(=|,)?\s*\d", content
    ), "rftp_watts must stay NULL: no numeric value should be assigned to it"


def test_thresholds_fixture_athlete_insert_is_really_idempotent() -> None:
    """core.athlete has NO unique or exclusion constraints in frozen DDL.

    ``ON CONFLICT DO NOTHING`` would be ineffective there: each execution adds another
    athlete, and threshold INSERT (resolving athlete_id by display_name) creates one
    threshold per duplicate. Idempotence must use ``WHERE NOT EXISTS``.
    """
    content = THRESHOLDS_FIXTURE.read_text(encoding="utf-8")
    athlete_insert = next(
        b for b in re.findall(r"INSERT INTO.*?;", content, flags=re.S) if "core.athlete (" in b
    )

    assert "WHERE NOT EXISTS" in athlete_insert
    assert "ON CONFLICT" not in athlete_insert
    schema_ddl = (REPO_MIGRATIONS_DIR / "003_core_ref.sql").read_text(encoding="utf-8")
    athlete_ddl = re.search(
        r"CREATE TABLE core\.athlete\s*\(.*?\n\);",
        schema_ddl,
        flags=re.S,
    )
    assert athlete_ddl is not None
    assert "UNIQUE" not in athlete_ddl.group(
        0
    ), "core.athlete gained a unique constraint: this test and fixture need review"


# --- Advisory lock ---


class _FakeCursor:
    def __init__(self, result: bool | None) -> None:
        self._result = result
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[bool] | None:
        return None if self._result is None else (self._result,)


class _FakeConnection:
    def __init__(self, result: bool | None) -> None:
        self.cursor_obj = _FakeCursor(result)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_acquire_lock_uses_a_non_blocking_session_scoped_lock() -> None:
    """Non-blocking (``pg_try_advisory_lock``): a second runner in a container must
    fail immediately, not wait indefinitely. Session-scoped not transaction-scoped: the
    runner chains one transaction per migration, the lock must cover the sequence.
    """
    conn = _FakeConnection(result=True)

    acquire_lock(cast("Any", conn))

    sql, params = conn.cursor_obj.executed[0]
    assert "pg_try_advisory_lock" in sql
    assert "pg_advisory_xact_lock" not in sql
    assert params == (ADVISORY_LOCK_KEY,)


def test_acquire_lock_raises_when_another_runner_holds_it() -> None:
    conn = _FakeConnection(result=False)

    with pytest.raises(MigrationLockError):
        acquire_lock(cast("Any", conn))


def test_release_lock_unlocks_the_same_key() -> None:
    conn = _FakeConnection(result=True)

    release_lock(cast("Any", conn))

    # rollback() first: after a failed migration the connection is in aborted-transaction
    # state, where any command — including unlock — would fail.
    assert conn.rollbacks == 1
    sql, params = conn.cursor_obj.executed[0]
    assert "pg_advisory_unlock" in sql
    assert params == (ADVISORY_LOCK_KEY,)


def test_migration_008_fixes_three_activity_load_defects() -> None:
    """Guard on migration 008's three corrections.

    They are invisible at runtime: a NULL hr_tss and NULL intensity_factor raise
    no error, they silently empty entire columns. If someone recreates mart.activity_load
    without these two points someday, nothing protests—hence this test.
    """
    sql = (REPO_MIGRATIONS_DIR / "008_mart_activity_load_fix.sql").read_text(encoding="utf-8")

    # Defect 1: LTHR exists only on cross-discipline threshold row.
    assert "th_all.discipline = 'all'" in sql
    assert "coalesce(th.lthr_bpm, th_all.lthr_bpm)" in sql

    # Defect 2: without GAP branch, run IF is NULL as long as rftp_watts is.
    assert "r.ngp_speed_mps * r.ftp_pace_s_per_km / 1000.0" in sql
    assert "'run_gap'" in sql
    # Power branch stays priority: when validated rFTP arrives, it wins.
    assert sql.index("'run_power'") < sql.index("'run_gap'")

    # Defect 3: priority cascade READ from core.metric_method_priority, never copied.
    # Without safeguard, GAP branch on entire run discipline produces IF on treadmill
    # that priority matrix doesn't allow.
    assert "FROM core.metric_method_priority p" in sql
    assert "p.metric = 'tss'" in sql
    for method in ("bike_power", "run_power", "run_gap", "swim_css"):
        assert re.search(rf"'{method}' +=+ ANY\(b\.allowed_methods\)", sql), method

    # Value and method cannot diverge: one CASE resolves both, second just branches ON
    # result.
    assert "CASE r.intensity_factor_method" in sql

    # Migration 007 frozen: 008 fixes by replacing view, not rewriting.
    sql_007 = (REPO_MIGRATIONS_DIR / "007_mart.sql").read_text(encoding="utf-8")
    assert "th_all" not in sql_007
    assert "DROP MATERIALIZED VIEW mart.activity_load;" in sql


def _strip_sql_comments_and_literals(sql: str) -> str:
    """Strip -- comments and content of '...' literals.

    Lines preserved (none deleted) so error messages stay readable. Double quotes ''
    inside literal handled naturally: close then reopen, leaving content neutralized.
    """
    lines: list[str] = []
    in_literal = False
    for line in sql.splitlines():
        output: list[str] = []
        i = 0
        while i < len(line):
            char = line[i]
            if in_literal:
                if char == "'":
                    in_literal = False
                    output.append("'")
                i += 1
                continue
            if char == "'":
                in_literal = True
                output.append("'")
                i += 1
                continue
            if line.startswith("--", i):
                break
            output.append(char)
            i += 1
        lines.append("".join(output))
    return chr(10).join(lines)


def test_migration_008_brings_no_dependent_views() -> None:
    """DROP MATERIALIZED VIEW without CASCADE fails if another view reads
    mart.activity_load. Verified: daily_load, pmc, activity_wide, power_curve_best
    all read core.*, never mart.activity_load. Test fails if future migration creates
    dependency without updating 008."""
    others = [
        p for p in REPO_MIGRATIONS_DIR.glob("*.sql") if p.name != "008_mart_activity_load_fix.sql"
    ]
    for path in others:
        # Look for REAL SQL dependency, not mention. Comments -- and string literals
        # (COMMENT ON, which readily cite neighboring views) neutralized BEFORE search:
        # without this, documenting mart.activity_load fails view dependency test,
        # reflex becomes removing docs.
        code = _strip_sql_comments_and_literals(path.read_text(encoding="utf-8"))
        reads = [
            line
            for line in code.splitlines()
            if "mart.activity_load" in line
            and "CREATE MATERIALIZED VIEW" not in line
            and "CREATE UNIQUE INDEX" not in line
            and "COMMENT ON" not in line
        ]
        assert not reads, f"{path.name} reads mart.activity_load: {reads}"


def test_dependency_guard_ignores_comments_and_literals() -> None:
    """Counterproof of neutralizer: must silence prose AND see real SELECT.

    Guard just ignoring comments without more would go unnoticed—would never say
    anything again.
    """
    prose = "-- mart.activity_load mentioned here\nSELECT 1;\n"
    literal = "COMMENT ON VIEW x IS 'see mart.activity_load for audit';\n"
    real = "SELECT a.* FROM mart.activity_load a;\n"

    assert "mart.activity_load" not in _strip_sql_comments_and_literals(prose)
    assert "mart.activity_load" not in _strip_sql_comments_and_literals(literal)
    assert "mart.activity_load" in _strip_sql_comments_and_literals(real)


# --------------------------------------------------------------------------------------
# Migrations directory resolution
#
# The fallback (``<repository root>/migrations``) is derived from the source tree layout
# and is wrong once the package is installed in site-packages, which is exactly what the
# container image does. These tests pin the three-level precedence and, above all, the
# refusal to run against a directory that does not exist: an empty glob would otherwise
# be reported as "database is already up to date" while the schema is missing.
# --------------------------------------------------------------------------------------


def test_resolve_migrations_dir_prefers_explicit_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit argument wins over the environment variable."""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(MIGRATIONS_DIR_ENV_VAR, str(other))

    explicit = tmp_path / "explicit"
    explicit.mkdir()

    assert resolve_migrations_dir(explicit) == explicit


def test_resolve_migrations_dir_reads_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no argument, the environment variable is used. This is the container case."""
    monkeypatch.setenv(MIGRATIONS_DIR_ENV_VAR, str(tmp_path))

    assert resolve_migrations_dir(None) == tmp_path


def test_resolve_migrations_dir_falls_back_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With neither argument nor environment, the repository directory is used."""
    monkeypatch.delenv(MIGRATIONS_DIR_ENV_VAR, raising=False)

    assert resolve_migrations_dir(None) == REPO_MIGRATIONS_DIR


def test_resolve_migrations_dir_rejects_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing directory raises instead of silently yielding zero migrations."""
    monkeypatch.setenv(MIGRATIONS_DIR_ENV_VAR, str(tmp_path / "absent"))

    with pytest.raises(MigrationError, match="Migrations directory not found"):
        resolve_migrations_dir(None)


def test_discover_migrations_rejects_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal reaches the caller through discover_migrations, the single reader."""
    monkeypatch.delenv(MIGRATIONS_DIR_ENV_VAR, raising=False)

    with pytest.raises(MigrationError, match="Migrations directory not found"):
        discover_migrations(tmp_path / "absent")
