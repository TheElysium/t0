"""Indoor bike without heart rate chest strap (see profil-athlete.md: bike power only
on home-trainer, not always wearing strap). Verifies that missing heart rate doesn't
drift any calculation toward an invented 0 and that TSS remains calculable by power alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from t0.transform.activity import build_core_rows

if TYPE_CHECKING:
    from t0.transform.rows import CoreActivityBundle

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    bike_context,
    make_sample,
    parsed_source,
)

_DURATION_S = 600
_POWER_W = 176.0


def _bike_bundle() -> CoreActivityBundle:
    samples = [make_sample(t, power=int(_POWER_W)) for t in range(_DURATION_S)]
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


class TestIndoorBikeWithoutHeartRate:
    """Constant power 176 W for 10 min, FTP = 220 W (example threshold reference)."""

    def test_constant_power_gives_np_equal_to_average_power(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.avg_power == _POWER_W
        assert bundle.activity.np_watts == _POWER_W

    def test_missing_heart_rate_stays_none_never_zero(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.avg_hr is None
        assert bundle.activity.max_hr is None

    def test_power_only_tss_makes_needs_manual_rpe_false(self) -> None:
        bundle = _bike_bundle()
        tss_rows = {row.method_code: row for row in bundle.metrics if row.metric == "tss"}
        assert "bike_power" in tss_rows
        assert tss_rows["bike_power"].is_canonical
        assert tss_rows["bike_power"].value is not None
        assert bundle.activity.needs_manual_rpe is False

    def test_intensity_factor_reuses_same_method_code_as_canonical_tss(self) -> None:
        bundle = _bike_bundle()
        tss_canonical = next(
            row for row in bundle.metrics if row.metric == "tss" and row.is_canonical
        )
        if_rows = {
            row.method_code: row for row in bundle.metrics if row.metric == "intensity_factor"
        }
        assert tss_canonical.method_code in if_rows
        assert if_rows[tss_canonical.method_code].is_canonical is tss_canonical.is_canonical
        # IF = NP / FTP = 176 / 220 = 0.8, exactly.
        assert if_rows[tss_canonical.method_code].value == 0.8

    def test_efficiency_factor_and_decoupling_none_without_hr_never_zero(self) -> None:
        bundle = _bike_bundle()
        ef_rows = [row for row in bundle.metrics if row.metric == "efficiency_factor"]
        decoupling_rows = [row for row in bundle.metrics if row.metric == "decoupling_pct"]
        assert len(ef_rows) == 1
        assert len(decoupling_rows) == 1
        assert ef_rows[0].value is None
        assert ef_rows[0].is_canonical is False
        assert decoupling_rows[0].value is None

    def test_trimp_none_without_heart_rate_histogram(self) -> None:
        bundle = _bike_bundle()
        trimp_row = next(row for row in bundle.metrics if row.metric == "trimp")
        assert trimp_row.value is None
        assert trimp_row.is_canonical is False

    def test_np_reliable_true_beyond_reliability_threshold(self) -> None:
        # 600 s >= RELIABLE_THRESHOLD_S (300 s, see t0.compute.normalized_power).
        bundle = _bike_bundle()
        assert bundle.activity.np_reliable is True

    def test_total_samples_equals_raw_sample_count(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.total_samples == _DURATION_S

    def test_no_gradient_or_ngp_for_bike(self) -> None:
        bundle = _bike_bundle()
        assert bundle.activity.ngp_speed_mps is None
        assert bundle.activity.gap_applied is False
