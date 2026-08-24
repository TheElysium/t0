"""Self-contained test cases from 04-load-formulas.md §5.2 (hrTSS) + non-regression
against frozen seeds from core.hr_tss_coefficient (migrations/003_core_ref.sql)."""

from __future__ import annotations

import math
import re
from pathlib import Path

from garminator.load.hrtss import DEFAULT_HR_ZONE_COEFFICIENTS, hrtss_from_histogram

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _build_histogram(minutes_by_bpm: dict[int, int]) -> dict[int, int]:
    return {bpm: minutes * 60 for bpm, minutes in minutes_by_bpm.items()}


class TestHrTss:
    def test_hrtss_1_multi_zone_distribution(self) -> None:
        # LTHR=170 : Z1 <137.7, Z2 [137.7,153), Z3 [153,159.8), Z4 [159.8,170)
        histogram = _build_histogram({120: 15, 145: 30, 155: 10, 165: 5})
        value = hrtss_from_histogram(histogram, lthr_bpm=170)
        assert value is not None
        assert math.isclose(value, 52.5, rel_tol=1e-9)

    def test_hrtss_2_axiom_1h_in_z5a(self) -> None:
        histogram = _build_histogram({172: 60})  # 172/170 = 1.0117... in [1.00, 1.03)
        value = hrtss_from_histogram(histogram, lthr_bpm=170)
        assert value is not None
        assert math.isclose(value, 105.0, rel_tol=1e-9)

    def test_lthr_absent_yields_none_never_zero(self) -> None:
        histogram = _build_histogram({145: 60})
        assert hrtss_from_histogram(histogram, lthr_bpm=None) is None

    def test_empty_histogram_yields_none(self) -> None:
        assert hrtss_from_histogram({}, lthr_bpm=170) is None

    def test_increasing_with_intensity(self) -> None:
        low = hrtss_from_histogram(_build_histogram({120: 60}), lthr_bpm=170)
        high = hrtss_from_histogram(_build_histogram({172: 60}), lthr_bpm=170)
        assert low is not None
        assert high is not None
        assert high > low


class TestZoneTableNonDrift:
    """Falsifiability: this test fails if migration 003 is modified without propagating
    the change to DEFAULT_HR_ZONE_COEFFICIENTS (or vice versa)."""

    def test_python_constant_exactly_reflects_migration(self) -> None:
        sql = (_MIGRATIONS_DIR / "003_core_ref.sql").read_text(encoding="utf-8")
        match = re.search(
            r"INSERT INTO core\.hr_tss_coefficient.*?VALUES\s*(.*?)ON CONFLICT",
            sql,
            re.DOTALL,
        )
        assert match is not None, "hr_tss_coefficient INSERT block not found in migration"
        rows_text = match.group(1)
        row_pattern = re.compile(
            r"\(\s*'(?P<zone_code>[^']+)'\s*,\s*\d+\s*,\s*"
            r"(?P<lo>[\d.]+)\s*,\s*(?P<hi>[\d.]+)\s*,\s*(?P<rate>[\d.]+)\s*,"
        )
        parsed = [
            (
                m.group("zone_code"),
                float(m.group("lo")),
                float(m.group("hi")),
                float(m.group("rate")),
            )
            for m in row_pattern.finditer(rows_text)
        ]
        assert parsed, "no zone rows extracted: regex stopped matching migration"
        assert len(parsed) == len(DEFAULT_HR_ZONE_COEFFICIENTS)
        for (zone_code, lo, hi, rate), coeff in zip(
            parsed, DEFAULT_HR_ZONE_COEFFICIENTS, strict=True
        ):
            assert coeff.zone_code == zone_code
            assert math.isclose(coeff.lo_pct_lthr, lo, rel_tol=1e-9)
            assert math.isclose(coeff.hi_pct_lthr, hi, rel_tol=1e-9)
            assert math.isclose(coeff.tss_per_hour, rate, rel_tol=1e-9)


class TestHrTssDegenerate:
    def test_zero_lthr_yields_none(self) -> None:
        assert hrtss_from_histogram({180: 3600}, 0.0) is None

    def test_negative_lthr_yields_none(self) -> None:
        assert hrtss_from_histogram({180: 3600}, -170.0) is None

    def test_no_classifiable_seconds_yields_none_never_zero(self) -> None:
        """Zone table ends at 9.99 x LTHR. An aberrant bucket outside any zone must not
        produce hrTSS of 0.0, which would propagate into PMC."""
        assert hrtss_from_histogram({2500: 600}, 170.0) is None

    def test_aberrant_bucket_ignored_without_canceling_others(self) -> None:
        valid_only = hrtss_from_histogram({204: 3600}, 170.0)
        with_outlier = hrtss_from_histogram({204: 3600, 2500: 600}, 170.0)
        assert valid_only is not None
        assert with_outlier == valid_only
