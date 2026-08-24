"""Heart rate decoupling (``decoupling_pct``): bike power strictly constant over both
halves, but heart rate drifts notably upward in the second half -- textbook case of
cardiac drift (loss of efficiency despite unchanged mechanical effort). Verifies the
expected sign and order of magnitude, without locking in a fragile hand-calculated
value."""

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

_DURATION_S = 600
_POWER_W = 200
_HR_HALF1 = 130
_HR_HALF2 = 160  # clear drift: +30 bpm at unchanged power.


def _bundle(hr_half1: int, hr_half2: int) -> CoreActivityBundle:
    mid = _DURATION_S // 2
    samples = [
        make_sample(t, power=_POWER_W, heart_rate=hr_half1 if t < mid else hr_half2)
        for t in range(_DURATION_S)
    ]
    decoded = parsed_source(samples)
    return build_core_rows(
        decoded,
        bike_context(duration_s=_DURATION_S),
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )


class TestHeartRateDecouplingBikeDrift:
    def test_positive_decoupling_when_hr_drifts_at_constant_power(self) -> None:
        bundle = _bundle(_HR_HALF1, _HR_HALF2)
        row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        # EF1 = 200/130, EF2 = 200/160: EF2 < EF1 -> positive decoupling (degradation).
        assert row.value is not None
        assert row.value > 0

    def test_decoupling_equals_exactly_ef1_ef2_formula(self) -> None:
        bundle = _bundle(_HR_HALF1, _HR_HALF2)
        row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        ef1 = _POWER_W / _HR_HALF1
        ef2 = _POWER_W / _HR_HALF2
        expected = (ef1 - ef2) / ef1 * 100
        assert row.value is not None
        assert abs(row.value - expected) < 1e-6

    def test_zero_decoupling_when_heart_rate_stable(self) -> None:
        bundle = _bundle(_HR_HALF1, _HR_HALF1)
        row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        assert row.value is not None
        assert abs(row.value) < 1e-9

    def test_method_code_stays_bike_power_never_invented_code(self) -> None:
        bundle = _bundle(_HR_HALF1, _HR_HALF2)
        row = next(row for row in bundle.metrics if row.metric == "decoupling_pct")
        assert row.method_code == "bike_power"
        assert row.threshold_id is None
