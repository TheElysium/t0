"""Normalized power (NP, Coggan algorithm) - §1 of ``04-load-formulas.md``.

Windowing convention: **(A) full windows only**. A series of length ``N`` yields ``N - 29``
windows of 30 samples when ``N >= 30``; otherwise no window, and ``normalized_power``
returns ``None`` (never 0-see project rule: absence is ``None``, never fabricated).

Functions consume series **already regularized** at 1 Hz
(see :mod:`t0.compute.regularization`): zeros (coasting) retained, gaps already
filled or excised upstream. Functions have no knowledge of gaps-intentional, so test cases
("algorithm mechanics" from spec, ``p = [100, 200, 300, 400]``) transcribe directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Below this usable sample count, NP is undefined (§1.6, NP-0).
MIN_SAMPLES_FOR_NP = 30

#: Between these bounds (seconds usable), NP is mathematically defined but unreliable:
#: ``np_reliable`` must be ``False`` (§1.6, see ``core.activity.np_reliable``
#: in ``05-postgres-schema.md``).
RELIABLE_THRESHOLD_S = 300


def _rolling_means(values: Sequence[float], window: int) -> list[float]:
    """Rolling means over full windows only (convention A).

    Returns empty list if ``len(values) < window``.
    """
    n = len(values)
    if n < window:
        return []
    means: list[float] = []
    current_sum = sum(values[:window])
    means.append(current_sum / window)
    for i in range(window, n):
        current_sum += values[i] - values[i - window]
        means.append(current_sum / window)
    return means


def normalized_power(power_w: Sequence[float], *, window_s: int = 30) -> float | None:
    """Normalized power (W): 30-s rolling mean, 4th power, 4th root.

    ``power_w`` is power series in watts, already regularized at 1 Hz (coasting zeros retained).
    Returns ``None`` if fewer than ``window_s`` usable samples (§1.6, case NP-0)—never 0.
    Also returns ``None`` if series is **entirely zero** (§1.5): returning 0.0 would
    propagate as IF=0 then TSS=0 in L5, indistinguishable from a genuinely zero session.
    """
    means = _rolling_means(power_w, window_s)
    if not means:
        return None
    if not any(power_w):
        # §1.5: entirely zero series means "no usable power sensor", not "effort at 0 W".
        # Returning 0.0 would create IF=0 then TSS=0 in L5 (indistinguishable from zero session).
        return None
    fourth_power_mean = sum(m**4 for m in means) / len(means)
    return float(fourth_power_mean**0.25)


def np_reliable(sample_count: int, *, window_s: int = 30) -> bool | None:
    """NP reliability by usable sample count.

    - ``sample_count < window_s``: NP is ``None``, reliability undefined → ``None``.
    - ``window_s <= sample_count < RELIABLE_THRESHOLD_S``: NP defined but unreliable → ``False``.
    - ``sample_count >= RELIABLE_THRESHOLD_S``: → ``True``.
    """
    if sample_count < window_s:
        return None
    return sample_count >= RELIABLE_THRESHOLD_S


def average(values: Sequence[float]) -> float | None:
    """Simple arithmetic mean; ``None`` if empty (never 0)."""
    if not values:
        return None
    return sum(values) / len(values)


def average_nonzero(values: Sequence[float]) -> float | None:
    """Arithmetic mean of nonzero samples only; ``None`` if none."""
    nonzero = [v for v in values if v != 0]
    if not nonzero:
        return None
    return sum(nonzero) / len(nonzero)
