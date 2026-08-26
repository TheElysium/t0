"""Mirrors of the ``core.*`` tables produced by this stage (migrations 004 and 005).

Convention inherited without deviation from ``t0.fit.archive.ArchivedFile`` and
``t0.load.activity_load.ActivityMetricRow``: one field, one column, same name,
so that turning a row into an ``INSERT`` is a plain projection with no hidden
transformation. ``NULL`` is a MEANINGFUL value in this schema ("not computable", never
conflated with 0 — see the comment on ``core.activity_metric.value`` in
migrations/005_core_series.sql): no function here may replace a legitimate ``None``
with 0.

Three DDL tables deliberately get no new dataclass here:

- ``core.sample`` / ``core.activity_lap`` / ``core.swim_length`` are already mirrored,
  column for column, by ``t0.fit.model.SampleRow`` / ``LapRow`` /
  ``SwimLengthRow``. Redefining them would duplicate what the previous stage produced
  with the explicit intent of being reused as-is.
- ``core.activity_metric`` is mirrored by
  ``t0.load.activity_load.ActivityMetricRow``, re-exported here for the same
  reason.

``core.device`` and ``core.sample_derived`` DO get a dataclass below, but
``build_core_rows`` (``t0.transform.activity``) does not populate them: the L3
stage decodes no ``device_info`` FIT message, and populating ``sample_derived``
correctly would require freezing a second-by-second alignment convention that the
current ``mart`` views do not need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from t0.load.activity_load import ActivityMetricRow

if TYPE_CHECKING:
    from datetime import date, datetime

    from t0.fit.model import LapRow, SampleRow, SwimLengthRow

__all__ = [
    "ActivityHrHistogramRow",
    "ActivityMetricRow",
    "ActivityPowerCurveRow",
    "ActivityRow",
    "CoreActivityBundle",
    "DeviceRow",
    "SampleDerivedRow",
]


@dataclass(frozen=True, slots=True)
class DeviceRow:
    """Mirror of ``core.device``. Not populated here (see module docstring)."""

    manufacturer: str | None = None
    product: str | None = None
    serial_number: str | None = None
    device_type: str | None = None
    software_version: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """Mirror of ``core.activity``. See migrations/004_core_activity.sql.

    ``ingested_at`` / ``updated_at`` are deliberately absent: their columns carry
    ``DEFAULT now()`` in the database, and the write instant only exists at ``INSERT``
    time.
    """

    activity_id: int
    athlete_id: int
    sport_code: str
    start_time: datetime
    start_time_local: datetime
    local_date: date
    tz_offset_s: int
    duration_s: int
    garmin_type_key: str | None = None
    elapsed_s: int | None = None
    moving_s: int | None = None
    distance_m: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_power: float | None = None
    max_power: float | None = None
    avg_power_nonzero: float | None = None
    np_watts: float | None = None
    np_reliable: bool | None = None
    avg_speed_mps: float | None = None
    avg_cadence: float | None = None
    calories: int | None = None
    ngp_speed_mps: float | None = None
    gap_applied: bool = False
    swim_time_s: float | None = None
    swim_time_source: str | None = None
    total_samples: int | None = None
    imputed_seconds: int = 0
    excised_seconds: int = 0
    data_quality: str = "unknown"
    parent_activity_id: int | None = None
    is_parent: bool = False
    leg_order: int | None = None
    primary_device_id: int | None = None
    fit_file_id: int | None = None
    summary_sha256: bytes | None = None
    name: str | None = None
    needs_manual_rpe: bool = False
    source: str = "garmin"
    """``core.activity.source`` (migration 014): ``'garmin'`` or ``'manual'``. Defaulted
    to ``'garmin'`` so every existing caller that does not know about manual uploads
    keeps building a valid row unchanged."""


@dataclass(frozen=True, slots=True)
class SampleDerivedRow:
    """Mirror of ``core.sample_derived``. Not populated here (see module docstring)."""

    activity_id: int
    t: datetime
    power_30s: float | None = None
    grade_pct: float | None = None
    grade_factor: float | None = None
    gap_speed_mps: float | None = None
    imputed: bool = False
    excised: bool = False


@dataclass(frozen=True, slots=True)
class ActivityHrHistogramRow:
    """Mirror of one ``core.activity_hr_histogram`` row (1 bpm resolution)."""

    activity_id: int
    hr_bpm: int
    seconds: int


@dataclass(frozen=True, slots=True)
class ActivityPowerCurveRow:
    """Mirror of one ``core.activity_power_curve`` row.

    ``kind`` must be one of the values allowed by the ``apc_kind_ck`` constraint:
    ``bike_power`` / ``run_power`` / ``run_speed`` / ``swim_speed`` / ``heart_rate``.
    """

    activity_id: int
    kind: str
    duration_s: int
    value: float
    spans_pause: bool = False


@dataclass(frozen=True, slots=True)
class CoreActivityBundle:
    """Everything ``build_core_rows`` produces for ONE activity, ready to be written.

    ``samples`` / ``laps`` / ``swim_lengths`` are ``t0.fit.model`` types reused
    as-is. ``sample_derived`` is always empty (see module docstring).
    """

    activity: ActivityRow
    samples: tuple[SampleRow, ...]
    laps: tuple[LapRow, ...]
    swim_lengths: tuple[SwimLengthRow, ...]
    hr_histogram: tuple[ActivityHrHistogramRow, ...]
    power_curve: tuple[ActivityPowerCurveRow, ...]
    metrics: tuple[ActivityMetricRow, ...]
    sample_derived: tuple[SampleDerivedRow, ...] = field(default_factory=tuple)
