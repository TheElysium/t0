"""Banister TRIMP (04-formules-charge.md §5.1) from HR histogram.

Computed per histogram bucket (1 bpm resolution, ``core.activity_hr_histogram``), not
global mean: mean(f(HR)) ≠ f(mean(HR)) (Jensen inequality, TRIMP convex in HR). Spec
recommends this variant (04 §5.1.3); only directly computable from L4/L3 output
(no raw HR series outside histogram + samples).

Non-negotiable rule: without usable HR, TRIMP = None, never 0—zero reads as "no effort"
which is wrong (missing data).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

Sex = Literal["male", "female"]

_SECONDS_PER_MINUTE = 60.0
_MALE_COEFFICIENT = 0.64
_MALE_EXPONENT_FACTOR = 1.92
_FEMALE_COEFFICIENT = 0.86
_FEMALE_EXPONENT_FACTOR = 1.67


def _hrr(hr_bpm: float, hr_rest_bpm: float, hr_max_bpm: float) -> float:
    """Heart rate reserve fraction, clamped to [0, 1] (04 §5.1.2: HRR<0 → 0; HRR>1 → 1;
    latter should trigger upstream signal, outside this pure function scope)."""
    span = hr_max_bpm - hr_rest_bpm
    raw = (hr_bpm - hr_rest_bpm) / span
    return min(max(raw, 0.0), 1.0)


def _banister_k(hrr: float, sex: Sex) -> float:
    if sex == "male":
        return _MALE_COEFFICIENT * math.exp(_MALE_EXPONENT_FACTOR * hrr)
    return _FEMALE_COEFFICIENT * math.exp(_FEMALE_EXPONENT_FACTOR * hrr)


def trimp_from_histogram(
    histogram: Mapping[int, int],
    hr_rest_bpm: float | None,
    hr_max_bpm: float | None,
    sex: Sex = "male",
) -> float | None:
    """TRIMP = sum over buckets of (bucket_minutes x HRR x k(HRR, sex)).

    Returns None (never 0.0) if histogram empty or rest/max HR thresholds absent or
    inconsistent (hr_max ≤ hr_rest): cardiac load simply uncomputable, not zero load.
    """
    if hr_rest_bpm is None or hr_max_bpm is None or hr_max_bpm <= hr_rest_bpm:
        return None
    if not histogram:
        return None
    total = 0.0
    for hr_bpm, seconds in histogram.items():
        hrr = _hrr(float(hr_bpm), hr_rest_bpm, hr_max_bpm)
        k = _banister_k(hrr, sex)
        total += (seconds / _SECONDS_PER_MINUTE) * hrr * k
    return total
