"""Power sensor gap: verifies ``imputed_seconds``/``excised_seconds``/``data_quality``
(derived from power channel only, see ``core.activity.imputed_seconds`` docstring in
migrations/004) and that raw averages (``avg_power``/``avg_power_nonzero``/``max_power``)
remain calculated on the NON-imputed series, to avoid polluting the coasting-vs-sensor-loss
diagnosis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminator.transform.activity import build_core_rows

if TYPE_CHECKING:
    from garminator.transform.rows import CoreActivityBundle

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    bike_context,
    make_sample,
    parsed_source,
)

_GAP_S = 40  # > max_interp_gap_s (3 s) -> impute to 0, never interpolate.


def _bundle_with_gap() -> CoreActivityBundle:
    before = [make_sample(t, power=200) for t in range(10)]
    after = [make_sample(t, power=200) for t in range(10 + _GAP_S, 10 + _GAP_S + 10)]
    samples = before + after
    decoded = parsed_source(samples)
    return build_core_rows(
        decoded,
        bike_context(duration_s=10 + _GAP_S + 10),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestPowerSensorGap:
    def test_imputed_seconds_reflects_exactly_the_gap(self) -> None:
        bundle = _bundle_with_gap()
        assert bundle.activity.imputed_seconds == _GAP_S
        assert bundle.activity.excised_seconds == 0  # no pause declared by caller.

    def test_data_quality_degraded_to_poor_beyond_10_percent_imputed(self) -> None:
        bundle = _bundle_with_gap()
        # 40 s imputed out of 60 s total (10 + 40 + 10) = 66% > 10% threshold.
        assert bundle.activity.data_quality == "poor"

    def test_raw_averages_ignore_imputed_seconds(self) -> None:
        bundle = _bundle_with_gap()
        # All 20 real samples are 200 W: raw average must NOT be pulled down
        # by the 40 s at 0 W injected for regularization.
        assert bundle.activity.avg_power == 200.0
        assert bundle.activity.avg_power_nonzero == 200.0
        assert bundle.activity.max_power == 200.0

    def test_np_watts_calculated_on_regularized_series_thus_impacted_by_gap(self) -> None:
        bundle = _bundle_with_gap()
        # Intentional contrast with the prior test: NP MUST reflect the gap
        # (it's the regularized channel that feeds NP, by batch L4 design).
        assert bundle.activity.np_watts is not None
        assert bundle.activity.np_watts < 200.0

    def test_np_reliable_false_below_reliability_threshold_but_not_none(self) -> None:
        bundle = _bundle_with_gap()
        # 60 s of regularized series: >= window_s (30) but < RELIABLE_THRESHOLD_S (300).
        assert bundle.activity.np_reliable is False
