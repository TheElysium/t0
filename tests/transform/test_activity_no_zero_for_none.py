"""Transversal regression test: on an activity entirely devoid of usable data (no
samples), NO "non-calculable" field of ``ActivityRow`` must be 0 -- only ``None``
represents an incalculable quantity in this schema (see ``garminator.transform.rows``
docstring and comment on ``core.activity_metric.value`` in migrations/005_core_series.sql).

This test explicitly sweeps nullable DDL fields -- it intentionally excludes counters
that legitimately have 0 as a meaningful default value (``imputed_seconds``/
``excised_seconds``/``gap_applied``/``is_parent``/``needs_manual_rpe``), which are
NOT "non-calculable" quantities but counters/booleans whose 0/False is the real value
in the absence of anomaly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.transform.activity import build_core_rows

if TYPE_CHECKING:
    from garminator.transform.rows import CoreActivityBundle

from .conftest import ALL_THRESHOLD, ATHLETE_ID, BIKE_THRESHOLD, bike_context, parsed_source

#: Nullable fields of core.activity (migrations/004) that represent an "incalculable"
#: quantity -- never a counter or boolean flag with a meaningful default.
_NULLABLE_METRIC_FIELDS = (
    "distance_m",
    "ascent_m",
    "descent_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "max_power",
    "avg_power_nonzero",
    "np_watts",
    "np_reliable",
    "avg_speed_mps",
    "avg_cadence",
    "calories",
    "ngp_speed_mps",
    "swim_time_s",
    "swim_time_source",
)


def _empty_bike_bundle() -> CoreActivityBundle:
    decoded = parsed_source([])
    return build_core_rows(
        decoded,
        bike_context(duration_s=3600),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestNoZeroSubstitutedForNoneOnEmptyActivity:
    def test_all_nullable_fields_of_activity_row_are_none(self) -> None:
        bundle = _empty_bike_bundle()
        for field_name in _NULLABLE_METRIC_FIELDS:
            value = getattr(bundle.activity, field_name)
            assert value is None, f"{field_name} is {value!r}, expected None"

    def test_legitimate_counters_remain_at_default_zero(self) -> None:
        bundle = _empty_bike_bundle()
        assert bundle.activity.imputed_seconds == 0
        assert bundle.activity.excised_seconds == 0
        assert bundle.activity.total_samples == 0

    def test_no_activity_metric_row_is_zero_all_are_none(self) -> None:
        bundle = _empty_bike_bundle()
        for row in bundle.metrics:
            assert row.value is None, f"{row.metric}/{row.method_code} is {row.value!r}"
            assert row.is_canonical is False

    def test_needs_manual_rpe_true_because_no_calculated_method_produced_tss(self) -> None:
        bundle = _empty_bike_bundle()
        assert bundle.activity.needs_manual_rpe is True
