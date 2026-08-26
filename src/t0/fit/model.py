"""Intermediate structure shared by FIT and TCX readers.

Single type set for both input formats: design constraint from addendum
(addendum §2)—"two distinct readers producing same
intermediate structure, one write path behind". Fields match exactly columns in
``core.sample`` / ``core.activity_lap`` / ``core.swim_length`` tables (migrations 004, 005)
so passage to ``INSERT`` (out of this batch scope) is pure field projection, no hidden transform.

No class carries ``activity_id`` hardcoded in decode: source file (FIT or TCX) doesn't know
Garmin activity identifier, provided by caller (API JSON summary, see L2/L6). Functions
``decode_fit`` / ``decode_tcx`` receive it as parameter rather than inventing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class SampleRow:
    """Row for ``core.sample``. See migrations/005_core_series.sql."""

    activity_id: int
    t: datetime
    elapsed_s: int
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    altitude_m: float | None = None
    distance_m: float | None = None
    speed_mps: float | None = None
    heart_rate: int | None = None
    cadence: int | None = None
    power: int | None = None
    temperature: int | None = None
    vertical_oscillation: float | None = None
    stance_time: float | None = None
    step_length: float | None = None
    vertical_ratio: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LapRow:
    """Row for ``core.activity_lap``. See migrations/004_core_activity.sql.

    ``np_watts`` / ``np_convention`` intentionally absent: ``core.activity.np_watts`` comment
    is explicit ("NEVER read from FIT field"). Normalized NP is L4 calculation, never L3 read.
    """

    activity_id: int
    lap_index: int
    start_time: datetime
    duration_s: int
    elapsed_s: int | None = None
    distance_m: float | None = None
    avg_speed_mps: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_power: float | None = None
    max_power: float | None = None
    avg_cadence: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None
    intensity: str | None = None
    lap_trigger: str | None = None
    swim_stroke: str | None = None
    num_lengths: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SwimLengthRow:
    """Row for ``core.swim_length``. See migrations/004_core_activity.sql.

    ``duration_s`` is ``float`` not ``int``: SQL column is ``real``, justified in
    migration comment by FIT ``length`` message centisecond precision (rounding would
    drift over 40 lengths).
    """

    activity_id: int
    length_index: int
    start_time: datetime
    duration_s: float
    length_type: str
    lap_index: int | None = None
    swim_stroke: str | None = None
    total_strokes: int | None = None
    avg_cadence: float | None = None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Comparison fields from FIT ``session`` message, never truth source.

    Verified (addendum §1): ``training_stress_score`` absent from both real FITs. Fields
    below ARE present, must be kept for comparison, never used as canonical load
    (research/05-postgres-schema.md §8.1). No ``core`` table accepts them in current frozen
    DDL: class exists so caller (L2/L6, out of L3 scope) decides where to store them,
    rather than L3 discarding them meanwhile.
    """

    training_load_peak: float | None = None
    total_training_effect: float | None = None
    total_anaerobic_training_effect: float | None = None
    workout_rpe: float | None = None
    workout_feel: float | None = None
    training_stress_score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedActivitySource:
    """Shared output from both readers: what L3 knows and doesn't know.

    ``session`` is ``None`` for TCX (no ``session`` message—addendum §2.2): no value
    invented to fill absence, per "make explicit what it doesn't know, don't invent zeros".
    """

    source_format: str  # "fit" or "tcx"
    samples: list[SampleRow]
    laps: list[LapRow]
    swim_lengths: list[SwimLengthRow]
    session: SessionSummary | None
    start_time: datetime | None
    """Earliest known absolute instant in activity (session.start_time for FIT,
    first sample timestamp for TCX). ``None`` only if file contains no usable samples."""
    tcx_sport: str | None = None
    """Root ``Activity`` element's ``Sport`` attribute, TCX only ("Running" | "Biking" |
    "Other" per the TCX schema). Added here rather than as a new dataclass because this
    frozen dataclass IS the shared contract between both readers: an optional field with
    a default keeps the FIT reader, which never sets it, unchanged. Always ``None`` for
    FIT -- the FIT ``session`` message already carries sport/sub_sport in ``.raw``."""
