"""Assemble ``core.activity_metric`` rows for an activity (L5 orchestration).

Ties tss.py (formulas), trimp.py, hrtss.py, and method_priority.py (canonical
resolution) into rows ready for upsert into ``core.activity_metric``. This module never
touches the database: effective write (upsert, transaction) belongs to the ingestion layer,
outside this scope.

``rpe`` (manual entry) is not computed here: it does not appear in emitted rows but stays
in the priority chain so :func:`needs_manual_rpe` can detect when no computed method
produced a value (caller must then queue the activity for manual entry,
see ``core.activity.needs_manual_rpe``).

UNRESOLVED DUPLICATION (transform layer): ``mart.activity_load`` (migrations/008)
recalculates ``intensity_factor`` INDEPENDENTLY in SQL with its own column
``intensity_factor_method``. This module now emits a second version in ``core.activity_metric``
(see :func:`build_activity_metric_rows`): two definitions of the same number coexist.
Both are numerically equivalent by construction (same threshold, same canonical method),
but the coexistence itself was never resolved—neither removed nor marked as intentional
in DDL. Left for human decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from t0.load import tss as tss_formulas
from t0.load.hrtss import hrtss_from_histogram
from t0.load.method_priority import resolve_canonical
from t0.load.thresholds import Threshold, pace_s_per_km_to_speed_mps
from t0.load.trimp import Sex, trimp_from_histogram

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class ActivityLoadInput:
    """Scalar inputs needed for activity load calculation.

    ``np_watts`` and ``ngp_speed_mps`` are quantities produced by L4 (``t0.compute``)
    and received here as plain floats, no code dependency on that layer (L4 -> L5 contract)."""

    activity_id: int
    sport_code: str
    discipline: str  # 'bike' | 'run' | 'swim' | 'other'
    duration_s: float
    distance_m: float | None
    swim_time_s: float | None
    np_watts: float | None
    ngp_speed_mps: float | None
    hr_histogram: Mapping[int, int]
    threshold: Threshold | None
    all_threshold: Threshold | None
    sex: Sex = "male"


@dataclass(frozen=True, slots=True)
class ActivityMetricRow:
    """Mirror of a ``core.activity_metric`` row."""

    activity_id: int
    metric: str
    method_code: str
    value: float | None
    is_canonical: bool
    threshold_id: int | None


def build_activity_metric_rows(data: ActivityLoadInput) -> tuple[ActivityMetricRow, ...]:
    """Compute all TSS methods applicable to discipline, resolve canonical method via
    ``core.metric_method_priority``, add TRIMP row and ``intensity_factor`` row for
    each retained TSS method.

    ``intensity_factor`` has no row in ``core.metric_method_priority`` (only ``tss`` does),
    so no second :func:`resolve_canonical` call for it. Canonical method and priority
    order are REUSED directly from ``tss`` resolution (same ``method_code``, same
    ``is_canonical``)—same method_code and canonical resolution as corresponding TSS.
    ``hr_hrtss`` never has IF (hrTSS formula is not magnitude/threshold ratio,
    see ``hrtss.py`` returns only TSS): method stays in TSS chain but produces no
    ``intensity_factor`` row.
    """
    lthr = data.all_threshold.lthr_bpm if data.all_threshold else None
    hr_rest = data.all_threshold.hr_rest_bpm if data.all_threshold else None
    hr_max = data.all_threshold.hr_max_bpm if data.all_threshold else None
    all_threshold_id = data.all_threshold.threshold_id if data.all_threshold else None
    discipline_threshold_id = data.threshold.threshold_id if data.threshold else None

    # One IntensityResult per computed method (never for hr_hrtss): stored separately
    # from method_values to avoid recomputing any formula twice between tss and
    # intensity_factor rows—single source of truth per method (mirrors SQL decision
    # in migrations/008_mart_activity_load_fix.sql).
    intensity_results: dict[str, tss_formulas.IntensityResult] = {}
    method_values: dict[str, float | None] = {
        "hr_hrtss": hrtss_from_histogram(data.hr_histogram, lthr)
    }

    if data.discipline == "bike":
        ftp = data.threshold.ftp_watts if data.threshold else None
        intensity_results["bike_power"] = tss_formulas.bike_tss(data.duration_s, data.np_watts, ftp)
        method_values["bike_power"] = intensity_results["bike_power"].tss
    elif data.discipline == "run":
        rftp = data.threshold.rftp_watts if data.threshold else None
        intensity_results["run_power"] = tss_formulas.run_power_tss(
            data.duration_s, data.np_watts, rftp
        )
        method_values["run_power"] = intensity_results["run_power"].tss
        ftp_pace = data.threshold.ftp_pace_s_per_km if data.threshold else None
        ftpa_speed = pace_s_per_km_to_speed_mps(ftp_pace)
        intensity_results["run_gap"] = tss_formulas.run_gap_tss(
            data.duration_s, data.ngp_speed_mps, ftpa_speed
        )
        method_values["run_gap"] = intensity_results["run_gap"].tss
    elif data.discipline == "swim":
        css = data.threshold.css_m_s if data.threshold else None
        intensity_results["swim_css"] = tss_formulas.swim_tss(
            data.swim_time_s, data.distance_m, css
        )
        method_values["swim_css"] = intensity_results["swim_css"].tss

    resolved = resolve_canonical("tss", data.sport_code, method_values)

    def _threshold_id_for(method_code: str) -> int | None:
        return all_threshold_id if method_code == "hr_hrtss" else discipline_threshold_id

    applicable = [result for result in resolved if result.method_code in method_values]

    rows = [
        ActivityMetricRow(
            activity_id=data.activity_id,
            metric="tss",
            method_code=result.method_code,
            value=result.value,
            is_canonical=result.is_canonical,
            threshold_id=_threshold_id_for(result.method_code),
        )
        for result in applicable
    ]
    rows.extend(
        ActivityMetricRow(
            activity_id=data.activity_id,
            metric="intensity_factor",
            method_code=result.method_code,
            value=intensity_results[result.method_code].intensity_factor,
            is_canonical=result.is_canonical,
            threshold_id=_threshold_id_for(result.method_code),
        )
        for result in applicable
        if result.method_code in intensity_results
    )

    trimp_value = trimp_from_histogram(data.hr_histogram, hr_rest, hr_max, data.sex)
    rows.append(
        ActivityMetricRow(
            activity_id=data.activity_id,
            metric="trimp",
            method_code="hr_trimp",
            value=trimp_value,
            is_canonical=trimp_value is not None,
            threshold_id=all_threshold_id,
        )
    )
    return tuple(rows)


def needs_manual_rpe(rows: tuple[ActivityMetricRow, ...]) -> bool:
    """True when no computed method produced canonical TSS: only ``rpe`` (manual entry)
    remains in priority chain, not computed here. Mirrors ``core.activity.needs_manual_rpe``."""
    return not any(row.is_canonical for row in rows if row.metric == "tss")
