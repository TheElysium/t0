"""Immutable computations at ingestion (batch L4).

Pure functions, no disk/network, operating on in-memory sequences
(Sequence[float] / Sequence[int] / Sequence[float | None]). This module is the
public contract consumed by L5 (training load, TSS/rTSS/sTSS/hrTSS, CTL/ATL/TSB):
L5 imports only the re-exports here, as scalars (float | None), never submodule
internals.

Units, explicit for each function:
- power: watts (W)
- speed: meters/second (m/s)
- pace: seconds per kilometer (s/km)
- duration: seconds (s)
- heart rate: beats/minute (bpm)
- grade: dimensionless fraction (0.05 = 5%)

Golden rule (project-wide): a value that cannot be computed from available data
returns None, never a fabricated value (0, an estimated average, a guessed
threshold). No function in this module estimates physiological thresholds (FTP,
rFTP, LTHR, CSS): thresholds are inputs, never outputs, of this batch.
"""

from __future__ import annotations

from t0.compute.decoupling import decoupling_pct, efficiency_factor
from t0.compute.hr_histogram import hr_histogram
from t0.compute.minetti import (
    grade_adjusted_pace_s_per_km,
    grade_adjusted_speed_mps,
    grade_adjustment_factor,
    normalized_graded_pace_s_per_km,
    normalized_graded_pace_speed_mps,
    running_energy_cost,
    smooth_grade_series,
)
from t0.compute.normalized_power import (
    average,
    average_nonzero,
    normalized_power,
    np_reliable,
)
from t0.compute.power_curve import (
    best_effort,
    best_effort_spans_pause,
    log_spaced_durations,
    power_duration_curve,
)
from t0.compute.regularization import (
    Gap,
    RegularizedSeries,
    detect_gaps,
    regularize_series,
)

__all__ = [
    "Gap",
    "RegularizedSeries",
    "average",
    "average_nonzero",
    "best_effort",
    "best_effort_spans_pause",
    "decoupling_pct",
    "detect_gaps",
    "efficiency_factor",
    "grade_adjusted_pace_s_per_km",
    "grade_adjusted_speed_mps",
    "grade_adjustment_factor",
    "hr_histogram",
    "log_spaced_durations",
    "normalized_graded_pace_s_per_km",
    "normalized_graded_pace_speed_mps",
    "normalized_power",
    "np_reliable",
    "power_duration_curve",
    "regularize_series",
    "running_energy_cost",
    "smooth_grade_series",
]
