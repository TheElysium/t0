"""Cas numériques auto-portants de 04-formules-charge.md §5.2 (hrTSS) + non-régression
contre les seeds figées de core.hr_tss_coefficient (migrations/003_core_ref.sql)."""

from __future__ import annotations

import math
import re
from pathlib import Path

from garminator.load.hrtss import DEFAULT_HR_ZONE_COEFFICIENTS, hrtss_from_histogram

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _build_histogram(minutes_by_bpm: dict[int, int]) -> dict[int, int]:
    return {bpm: minutes * 60 for bpm, minutes in minutes_by_bpm.items()}


class TestHrTss:
    def test_hrtss_1_repartition_multi_zones(self) -> None:
        # LTHR=170 : Z1 <137.7, Z2 [137.7,153), Z3 [153,159.8), Z4 [159.8,170)
        histogram = _build_histogram({120: 15, 145: 30, 155: 10, 165: 5})
        value = hrtss_from_histogram(histogram, lthr_bpm=170)
        assert value is not None
        assert math.isclose(value, 52.5, rel_tol=1e-9)

    def test_hrtss_2_axiome_1h_en_z5a(self) -> None:
        histogram = _build_histogram({172: 60})  # 172/170 = 1.0117... in [1.00, 1.03)
        value = hrtss_from_histogram(histogram, lthr_bpm=170)
        assert value is not None
        assert math.isclose(value, 105.0, rel_tol=1e-9)

    def test_lthr_absent_donne_none_jamais_zero(self) -> None:
        histogram = _build_histogram({145: 60})
        assert hrtss_from_histogram(histogram, lthr_bpm=None) is None

    def test_histogramme_vide_donne_none(self) -> None:
        assert hrtss_from_histogram({}, lthr_bpm=170) is None

    def test_croissant_avec_intensite(self) -> None:
        low = hrtss_from_histogram(_build_histogram({120: 60}), lthr_bpm=170)
        high = hrtss_from_histogram(_build_histogram({172: 60}), lthr_bpm=170)
        assert low is not None
        assert high is not None
        assert high > low


class TestZoneTableNonDrift:
    """Falsifiabilité : ce test échoue si la migration 003 est modifiée sans répercuter
    le changement dans DEFAULT_HR_ZONE_COEFFICIENTS (ou l'inverse)."""

    def test_constante_python_reflete_exactement_la_migration(self) -> None:
        sql = (_MIGRATIONS_DIR / "003_core_ref.sql").read_text(encoding="utf-8")
        match = re.search(
            r"INSERT INTO core\.hr_tss_coefficient.*?VALUES\s*(.*?)ON CONFLICT",
            sql,
            re.DOTALL,
        )
        assert match is not None, "bloc INSERT hr_tss_coefficient introuvable dans la migration"
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
        assert parsed, "aucune ligne de zone extraite : le regex a cesse de matcher la migration"
        assert len(parsed) == len(DEFAULT_HR_ZONE_COEFFICIENTS)
        for (zone_code, lo, hi, rate), coeff in zip(
            parsed, DEFAULT_HR_ZONE_COEFFICIENTS, strict=True
        ):
            assert coeff.zone_code == zone_code
            assert math.isclose(coeff.lo_pct_lthr, lo, rel_tol=1e-9)
            assert math.isclose(coeff.hi_pct_lthr, hi, rel_tol=1e-9)
            assert math.isclose(coeff.tss_per_hour, rate, rel_tol=1e-9)


class TestHrTssDegenerate:
    def test_lthr_nul_donne_none(self) -> None:
        assert hrtss_from_histogram({180: 3600}, 0.0) is None

    def test_lthr_negatif_donne_none(self) -> None:
        assert hrtss_from_histogram({180: 3600}, -170.0) is None

    def test_aucune_seconde_classable_donne_none_jamais_zero(self) -> None:
        """La table de zones s'arrête à 9.99 x LTHR. Un bucket aberrant hors de toute
        zone ne doit pas produire un hrTSS de 0.0, qui se propagerait dans la PMC."""
        assert hrtss_from_histogram({2500: 600}, 170.0) is None

    def test_bucket_aberrant_ignore_sans_annuler_les_autres(self) -> None:
        valid_only = hrtss_from_histogram({204: 3600}, 170.0)
        with_outlier = hrtss_from_histogram({204: 3600, 2500: 600}, 170.0)
        assert valid_only is not None
        assert with_outlier == valid_only
