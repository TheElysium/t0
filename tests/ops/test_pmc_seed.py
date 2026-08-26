"""Tests for the PMC seed computation (:mod:`t0.ops.pmc_seed`).

The database is faked rather than mocked loosely: the stub records the statements it was
given and replies with fixed rows, so a test that changes the SQL shape fails on the
recorded statement instead of silently passing on a mock that accepts anything.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from t0.ops.pmc_seed import (
    PmcSeed,
    PmcSeedError,
    compute_pmc_seed,
    format_pmc_seed,
    write_pmc_seed,
)


class _Cursor:
    def __init__(self, replies: list[Any], statements: list[str]) -> None:
        self._replies = replies
        self._statements = statements
        self._last: Any = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._statements.append(sql)
        self._last = self._replies.pop(0) if self._replies else None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> Any:
        return self._last


class _Conn:
    def __init__(self, replies: list[Any]) -> None:
        self._replies = replies
        self.statements: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self._replies, self.statements)


def _daily_rows(start: date, tss_by_offset: dict[int, float]) -> list[tuple[date, float]]:
    from datetime import timedelta

    return [(start + timedelta(days=off), tss) for off, tss in sorted(tss_by_offset.items())]


def test_missing_matview_is_an_explicit_error() -> None:
    conn = _Conn([None])
    with pytest.raises(PmcSeedError, match="does not exist"):
        compute_pmc_seed(conn, athlete_id=1)


def test_unpopulated_matview_refuses_to_seed_from_stale_data() -> None:
    conn = _Conn([(False,)])
    with pytest.raises(PmcSeedError, match="not populated"):
        compute_pmc_seed(conn, athlete_id=1)


def test_no_day_with_known_load_is_an_explicit_error() -> None:
    conn = _Conn([(True,), (None,)])
    with pytest.raises(PmcSeedError, match="no day with a known load"):
        compute_pmc_seed(conn, athlete_id=1)


def test_seed_date_defaults_to_first_day_with_known_load() -> None:
    start = date(2026, 5, 19)
    conn = _Conn([(True,), (start,), _daily_rows(start, {0: 42.0})])
    seed = compute_pmc_seed(conn, athlete_id=1)
    assert seed.seed_date == start
    assert seed.strategy == "stationary_42d"
    assert seed.window_days == 42


def test_days_absent_from_daily_load_count_as_zero() -> None:
    """The whole point: 42 TSS on one day out of 42 is a seed of 1.0, not of 42.0.

    Averaging over activity days alone returns the mean load of a SESSION, which is what
    makes a seed come out several times too high.
    """
    start = date(2026, 5, 19)
    conn = _Conn([(True,), (start,), _daily_rows(start, {0: 42.0})])
    seed = compute_pmc_seed(conn, athlete_id=1)
    assert seed.ctl == pytest.approx(1.0)
    assert seed.atl == pytest.approx(1.0)
    assert seed.days_with_load == 1


def test_ctl_and_atl_are_equal_by_construction() -> None:
    """A stationary seed assumes fitness and fatigue are balanced: both are the mean."""
    start = date(2026, 5, 19)
    rows = _daily_rows(start, {off: 70.0 for off in range(42)})
    conn = _Conn([(True,), (start,), rows])
    seed = compute_pmc_seed(conn, athlete_id=1)
    assert seed.ctl == pytest.approx(70.0)
    assert seed.ctl == seed.atl


def test_days_outside_the_window_are_ignored() -> None:
    start = date(2026, 5, 19)
    rows = _daily_rows(start, {0: 42.0, 41: 42.0, 42: 4200.0, 90: 4200.0})
    conn = _Conn([(True,), (start,), rows])
    seed = compute_pmc_seed(conn, athlete_id=1)
    assert seed.ctl == pytest.approx(2.0)


def test_explicit_seed_date_skips_the_lookup_query() -> None:
    start = date(2026, 5, 19)
    conn = _Conn([(True,), _daily_rows(start, {0: 42.0})])
    seed = compute_pmc_seed(conn, athlete_id=1, seed_date=start)
    assert seed.seed_date == start
    assert not any("min(local_date)" in stmt for stmt in conn.statements)


def test_non_default_window_is_not_labelled_stationary_42d() -> None:
    """The strategy name states the window length. A 14-day window stored as
    ``stationary_42d`` would be a lie that no later reader could detect."""
    start = date(2026, 5, 19)
    conn = _Conn([(True,), _daily_rows(start, {0: 42.0})])
    seed = compute_pmc_seed(conn, athlete_id=1, seed_date=start, window_days=14)
    assert seed.strategy == "manual"
    assert seed.window_days == 14


def test_null_tss_rows_are_dropped_rather_than_coerced() -> None:
    start = date(2026, 5, 19)
    rows: list[tuple[date, float | None]] = [(start, 42.0), (date(2026, 5, 20), None)]
    conn = _Conn([(True,), rows])
    seed = compute_pmc_seed(conn, athlete_id=1, seed_date=start)
    assert seed.days_with_load == 1


def test_write_uses_an_upsert_on_the_primary_key() -> None:
    conn = _Conn([])
    write_pmc_seed(
        conn,
        PmcSeed(
            athlete_id=1,
            seed_date=date(2026, 5, 19),
            ctl=42.0,
            atl=42.0,
            strategy="stationary_42d",
            days_with_load=42,
            window_days=42,
        ),
    )
    statement = conn.statements[0]
    assert "ON CONFLICT (athlete_id, seed_date) DO UPDATE" in statement
    assert "computed_at = now()" in statement


def test_format_reports_window_coverage() -> None:
    text = format_pmc_seed(
        PmcSeed(
            athlete_id=1,
            seed_date=date(2026, 5, 19),
            ctl=41.58,
            atl=41.58,
            strategy="stationary_42d",
            days_with_load=30,
            window_days=42,
        )
    )
    assert "seed_date=2026-05-19" in text
    assert "ctl=41.6" in text
    assert "30/42 day(s) with a known load" in text
