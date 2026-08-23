"""Canonical method resolution (core.metric_method_priority) + non-regression against
frozen DDL in migrations/003_core_ref.sql."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from garminator.load.method_priority import METRIC_METHOD_PRIORITY, resolve_canonical

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class TestResolveCanonical:
    def test_route_sans_capteur_retombe_sur_hr_hrtss_et_le_trace(self) -> None:
        """Central acceptance case: road bike without power → canonical hrTSS, method_code
        traced explicitly."""
        values = {"bike_power": None, "hr_hrtss": 65.0}
        resolved = resolve_canonical("tss", "cycling", values)
        by_method = {r.method_code: r for r in resolved}
        assert by_method["bike_power"].is_canonical is False
        assert by_method["hr_hrtss"].is_canonical is True
        assert by_method["hr_hrtss"].value == 65.0
        # Only one canonical row (mirrors unique partial index in DB).
        assert sum(r.is_canonical for r in resolved) == 1

    def test_puissance_disponible_prioritaire_sur_hr(self) -> None:
        """Falsifiability: if bike_power has value, must be canonical even if hr_hrtss
        also has value—otherwise priority not respected."""
        values = {"bike_power": 80.0, "hr_hrtss": 65.0}
        resolved = resolve_canonical("tss", "cycling", values)
        by_method = {r.method_code: r for r in resolved}
        assert by_method["bike_power"].is_canonical is True
        assert by_method["hr_hrtss"].is_canonical is False

    def test_course_rftp_null_retombe_sur_gap_puis_hr(self) -> None:
        """rFTP intentionally NULL (addendum §5): run_power ignored (None), run_gap
        chosen if available."""
        values = {"run_power": None, "run_gap": 70.0, "hr_hrtss": 50.0}
        resolved = resolve_canonical("tss", "running", values)
        by_method = {r.method_code: r for r in resolved}
        assert by_method["run_power"].is_canonical is False
        assert by_method["run_gap"].is_canonical is True
        assert by_method["hr_hrtss"].is_canonical is False

    def test_aucune_valeur_aucune_ligne_canonique(self) -> None:
        values = {"run_power": None, "run_gap": None, "hr_hrtss": None}
        resolved = resolve_canonical("tss", "running", values)
        assert not any(r.is_canonical for r in resolved)

    def test_sport_inconnu_leve_une_erreur(self) -> None:
        with pytest.raises(ValueError, match="no fallback method"):
            resolve_canonical("tss", "sport_inexistant", {})

    def test_valeur_manquante_dans_le_mapping_traitee_comme_none(self) -> None:
        resolved = resolve_canonical("tss", "cycling", {"hr_hrtss": 40.0})
        by_method = {r.method_code: r for r in resolved}
        assert by_method["bike_power"].value is None
        assert by_method["bike_power"].is_canonical is False
        assert by_method["hr_hrtss"].is_canonical is True


class TestPriorityTableNonDrift:
    """Falsifiability: test fails if migrations/003_core_ref.sql changed without
    updating METRIC_METHOD_PRIORITY (or vice versa)—intentionally desynchronize during
    test authoring (extra/missing row or changed priority fails assertion)."""

    def test_constante_python_reflete_exactement_la_migration(self) -> None:
        sql = (_MIGRATIONS_DIR / "003_core_ref.sql").read_text(encoding="utf-8")
        match = re.search(
            r"INSERT INTO core\.metric_method_priority.*?VALUES\s*(.*?)ON CONFLICT",
            sql,
            re.DOTALL,
        )
        assert match is not None, "INSERT metric_method_priority block not found in migration"
        rows_text = match.group(1)
        row_pattern = re.compile(
            r"\(\s*'(?P<metric>[^']+)'\s*,\s*'(?P<sport>[^']+)'\s*,\s*"
            r"'(?P<method>[^']+)'\s*,\s*(?P<priority>\d+)\s*\)"
        )
        parsed = {
            (m.group("metric"), m.group("sport"), m.group("method"), int(m.group("priority")))
            for m in row_pattern.finditer(rows_text)
        }
        assert parsed, "no rows extracted: regex stopped matching migration"
        assert parsed == set(METRIC_METHOD_PRIORITY)
        assert len(parsed) == len(METRIC_METHOD_PRIORITY)
