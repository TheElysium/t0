"""hrTSS from HR histogram x zone table (04-formules-charge.md §5.2).

``DEFAULT_HR_ZONE_COEFFICIENTS`` exactly mirrors seed rows of ``core.hr_tss_coefficient``
(migrations/003_core_ref.sql). Dedicated test (tests/load/test_hrtss.py) reparses
migration and compares to ensure no silent drift between this constant and frozen DDL.

hrTSS is fallback method for road bike (no power sensor): it enables all road bike
history; depends critically on cross-discipline threshold ``discipline='all'`` (LTHR).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class HrZoneCoefficient:
    zone_code: str
    lo_pct_lthr: float
    hi_pct_lthr: float
    tss_per_hour: float


#: Exact mirror of core.hr_tss_coefficient seeds (migrations/003_core_ref.sql).
DEFAULT_HR_ZONE_COEFFICIENTS: tuple[HrZoneCoefficient, ...] = (
    HrZoneCoefficient("Z1", 0.00, 0.81, 20.0),
    HrZoneCoefficient("Z2", 0.81, 0.90, 55.0),
    HrZoneCoefficient("Z3", 0.90, 0.94, 75.0),
    HrZoneCoefficient("Z4", 0.94, 1.00, 90.0),
    HrZoneCoefficient("Z5a", 1.00, 1.03, 105.0),
    HrZoneCoefficient("Z5b", 1.03, 1.07, 120.0),
    HrZoneCoefficient("Z5c", 1.07, 9.99, 140.0),
)


def _zone_for_pct(pct: float, zones: Sequence[HrZoneCoefficient]) -> HrZoneCoefficient | None:
    for zone in zones:
        if zone.lo_pct_lthr <= pct < zone.hi_pct_lthr:
            return zone
    return None


def hrtss_from_histogram(
    histogram: Mapping[int, int],
    lthr_bpm: float | None,
    zones: Sequence[HrZoneCoefficient] = DEFAULT_HR_ZONE_COEFFICIENTS,
) -> float | None:
    """hrTSS = sum over buckets of (bucket_hours x TSSperHour[zone(bucket)]).

    Mirrors SQL join from ``mart.activity_load`` (CTE hrtss):
    ``hr_bpm >= lo_pct_lthr*lthr AND hr_bpm < hi_pct_lthr*lthr``. Returns None
    (never 0.0) if LTHR is absent or histogram has no seconds classifiable in zone."""
    if lthr_bpm is None or lthr_bpm <= 0:
        return None
    if not histogram:
        return None
    total = 0.0
    matched_seconds = 0
    for hr_bpm, seconds in histogram.items():
        pct = hr_bpm / lthr_bpm
        zone = _zone_for_pct(pct, zones)
        if zone is None:
            continue
        matched_seconds += seconds
        total += (seconds / _SECONDS_PER_HOUR) * zone.tss_per_hour
    if matched_seconds == 0:
        return None
    return total
