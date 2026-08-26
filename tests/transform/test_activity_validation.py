"""Guardrails for ``build_core_rows``: two call inconsistencies that must be
detected rather than silently ignored (see function docstring)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from t0.transform.activity import build_core_rows

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    bike_context,
    make_sample,
    parsed_source,
)


class TestGuardrailsBuildCoreRows:
    def test_zero_or_negative_duration_raises_value_error(self) -> None:
        decoded = parsed_source([])
        with pytest.raises(ValueError, match="duration_s"):
            build_core_rows(
                decoded,
                bike_context(duration_s=0),
                athlete_id=ATHLETE_ID,
                fit_file_id=None,
                threshold=BIKE_THRESHOLD,
                all_threshold=ALL_THRESHOLD,
                sex="male",
            )

    def test_activity_id_mismatch_between_context_and_samples_raises_value_error(self) -> None:
        # A sample carrying the activity_id of a DIFFERENT activity: the kind of bug
        # we want to detect, not ignore (see build_core_rows docstring).
        sample = make_sample(0, power=200)
        wrong_activity_sample = replace(sample, activity_id=999)
        decoded = parsed_source([wrong_activity_sample])
        with pytest.raises(ValueError, match="activity_id"):
            build_core_rows(
                decoded,
                bike_context(activity_id=1, duration_s=600),
                athlete_id=ATHLETE_ID,
                fit_file_id=None,
                threshold=BIKE_THRESHOLD,
                all_threshold=ALL_THRESHOLD,
                sex="male",
            )
