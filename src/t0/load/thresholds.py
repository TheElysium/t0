"""Resolve threshold in effect at a date (core.threshold, 04 §2.3, migrations/003).

``core.threshold.validity`` is half-open ``daterange`` [valid_from, valid_to).
GiST constraint ``threshold_no_overlap`` forbids overlapping validities for same
discipline and athlete in DB. This module mirrors same search semantics on Python side,
never inventing threshold: uncovered date returns None (non-negotiable rule: never guess
threshold, see addendum §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_METERS_PER_KM = 1000.0


@dataclass(frozen=True, slots=True)
class Threshold:
    """Mirror of useful columns from ``core.threshold``. ``discipline`` is one of
    'bike', 'run', 'swim', 'all'; off-discipline columns are NULL by construction
    (CHECK constraints threshold_bike_only/run_only/swim_only)."""

    discipline: str
    valid_from: date
    valid_to: date | None
    threshold_id: int | None = None
    ftp_watts: float | None = None
    rftp_watts: float | None = None
    ftp_pace_s_per_km: float | None = None
    css_m_s: float | None = None
    lthr_bpm: float | None = None
    hr_max_bpm: float | None = None
    hr_rest_bpm: float | None = None

    def covers(self, on_date: date) -> bool:
        """Mirrors ``validity @> on_date`` for half-open interval [start, end)."""
        if on_date < self.valid_from:
            return False
        return self.valid_to is None or on_date < self.valid_to


def threshold_for_date(
    thresholds: Sequence[Threshold], discipline: str, on_date: date
) -> Threshold | None:
    """Threshold in effect for ``discipline`` on ``on_date``, or None if uncovered
    (e.g., activity before 2026-05-19 for this athlete: no threshold, no TSS—not an error,
    legitimate state to propagate, not hide with exception)."""
    matches = [t for t in thresholds if t.discipline == discipline and t.covers(on_date)]
    if len(matches) > 1:
        msg = (
            f"validity overlap detected for discipline={discipline} at {on_date}: "
            f"GiST constraint threshold_no_overlap should forbid it in DB, "
            "check data source"
        )
        raise ValueError(msg)
    return matches[0] if matches else None


def pace_s_per_km_to_speed_mps(pace_s_per_km: float | None) -> float | None:
    """Convert threshold pace (s/km, as stored in ``core.threshold.ftp_pace_s_per_km``)
    to speed (m/s) for rTSS-GAP calculation."""
    if pace_s_per_km is None or pace_s_per_km <= 0:
        return None
    return _METERS_PER_KM / pace_s_per_km
