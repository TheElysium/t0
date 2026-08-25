"""Canonical method resolution (core.metric_method_priority, migrations/003).

``METRIC_METHOD_PRIORITY`` exactly mirrors seed rows from migration: dedicated test
(tests/load/test_method_priority.py) reparses ``migrations/003_core_ref.sql`` and
compares to ensure no silent drift.

Semantics mirror partial unique index ``activity_metric_canonical_uq ON
core.activity_metric (activity_id, metric) WHERE is_canonical``: at most one canonical
row per (activity, metric), chosen as first method—in declared priority order—with
non-None value. This is how road bike without power sensor falls back to hr_hrtss."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: (metric, sport_code, method_code, priority) -- exact mirror of INSERTs into
#: core.metric_method_priority (migrations/003_core_ref.sql).
METRIC_METHOD_PRIORITY: tuple[tuple[str, str, str, int], ...] = (
    ("tss", "cycling", "bike_power", 1),
    ("tss", "cycling", "hr_hrtss", 2),
    ("tss", "cycling", "rpe", 3),
    ("tss", "virtual_cycling", "bike_power", 1),
    ("tss", "virtual_cycling", "hr_hrtss", 2),
    ("tss", "virtual_cycling", "rpe", 3),
    ("tss", "running", "run_power", 1),
    ("tss", "running", "run_gap", 2),
    ("tss", "running", "hr_hrtss", 3),
    ("tss", "trail_running", "run_power", 1),
    ("tss", "trail_running", "run_gap", 2),
    ("tss", "trail_running", "hr_hrtss", 3),
    ("tss", "treadmill", "run_power", 1),
    ("tss", "treadmill", "hr_hrtss", 2),
    ("tss", "lap_swimming", "swim_css", 1),
    ("tss", "lap_swimming", "hr_hrtss", 2),
    ("tss", "lap_swimming", "rpe", 3),
    ("tss", "open_water", "swim_css", 1),
    ("tss", "open_water", "hr_hrtss", 2),
    ("tss", "open_water", "rpe", 3),
    ("tss", "hiking", "hr_hrtss", 1),
    ("tss", "hiking", "rpe", 2),
    ("tss", "resort_skiing", "hr_hrtss", 1),
    ("tss", "resort_skiing", "rpe", 2),
    ("tss", "strength", "hr_hrtss", 1),
    ("tss", "strength", "rpe", 2),
    ("tss", "multisport", "hr_hrtss", 1),
    ("tss", "multisport", "rpe", 2),
    ("tss", "transition", "hr_hrtss", 1),
    ("tss", "transition", "rpe", 2),
)


@dataclass(frozen=True, slots=True)
class ResolvedMethod:
    method_code: str
    priority: int
    value: float | None
    is_canonical: bool


def resolve_canonical(
    metric: str,
    sport_code: str,
    values: Mapping[str, float | None],
    priority_table: Sequence[tuple[str, str, str, int]] = METRIC_METHOD_PRIORITY,
) -> tuple[ResolvedMethod, ...]:
    """One row per method declared for (metric, sport_code), sorted by increasing
    priority; ``is_canonical`` True on first with non-None value, False elsewhere
    (at most one canonical row, never false positives if value exists lower in priority).

    Raises ValueError if (metric, sport_code) has no declared method: configuration
    error (unknown sport_code), not silent None case.
    """
    applicable = sorted(
        (row for row in priority_table if row[0] == metric and row[1] == sport_code),
        key=lambda row: row[3],
    )
    if not applicable:
        msg = f"no fallback method declared for metric={metric} sport_code={sport_code}"
        raise ValueError(msg)

    canonical_chosen = False
    resolved: list[ResolvedMethod] = []
    for _, _, method_code, priority in applicable:
        value = values.get(method_code)
        is_canonical = value is not None and not canonical_chosen
        if is_canonical:
            canonical_chosen = True
        resolved.append(ResolvedMethod(method_code, priority, value, is_canonical))
    return tuple(resolved)
