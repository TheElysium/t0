"""Efficiency factor (EF) and aerobic decoupling — section 7 of 04-formules-charge.md.

EF = output / avg_HR, where output is NP (W) on bike or normalized GAP speed
(m/s) on run. Decoupling% = (EF1 - EF2) / EF1 x 100 between two equal-duration
halves of activity.

Sign convention (explicit because it differs from some TrainingPeaks displays):
positive decoupling means degradation (efficiency drops in second half—cardiac
drift). Negative decoupling would mean improvement, rare but valid (e.g.,
gradual warmup).
"""

from __future__ import annotations


def efficiency_factor(output: float | None, avg_hr: float | None) -> float | None:
    """Efficiency factor output / avg_hr.

    None if any input is missing or avg_hr <= 0.
    """
    if output is None or avg_hr is None or avg_hr <= 0:
        return None
    return output / avg_hr


def decoupling_pct(ef1: float | None, ef2: float | None) -> float | None:
    """Decoupling (%) between two halves: (EF1 - EF2) / EF1 x 100.

    None if either value is missing or EF1 == 0. Positive = second-half
    efficiency degradation (cardiac drift); negative = improvement.
    """
    if ef1 is None or ef2 is None or ef1 == 0:
        return None
    return (ef1 - ef2) / ef1 * 100
