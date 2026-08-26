"""Power-duration curve via prefix sums — §8 of ``04-load-formulas.md``.

Principle: for a 1 Hz series of length ``N``, best average effort over duration ``d`` is
``max_i (S[i+d] - S[i]) / d`` where ``S`` is the series prefix sum. O(N) per duration
via prefix sums (not naive sliding window O(N·d)).

Duration grid: log-spaced, ``points_per_decade`` points per decade, deduplicated
after rounding to nearest integer (§8.2). Generic: works for power (W), speed (m/s),
or HR (bpm)—caller chooses unit via series, see ``core.activity_power_curve.kind``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Number of durations per log decade (§8.2).
DEFAULT_POINTS_PER_DECADE = 20


def log_spaced_durations(
    min_s: int = 1,
    max_s: int = 21600,
    *,
    points_per_decade: int = DEFAULT_POINTS_PER_DECADE,
) -> list[int]:
    """Generate log-spaced, integer, strictly increasing, deduplicated duration grid.

    ``d_j = round(10^(j/points_per_decade))`` for increasing ``j``, kept if
    ``min_s <= d_j <= max_s``; duplicates from rounding removed while preserving order
    (§8.2: ~60-100 durations from 1 s to 6 h at 20 points/decade).

    If no log-grid point falls in ``[min_s, max_s]`` (narrow range between grid points,
    e.g. ``[60, 60]`` when grid jumps 56→63), grid shrinks to ``[min_s]`` not empty:
    explicit range never silently produces zero durations.
    """
    if min_s < 1:
        msg = "min_s must be >= 1"
        raise ValueError(msg)
    if max_s < min_s:
        msg = "max_s must be >= min_s"
        raise ValueError(msg)

    j_min = math.ceil(math.log10(min_s) * points_per_decade)
    j_max = math.floor(math.log10(max_s) * points_per_decade)

    durations: list[int] = []
    seen: set[int] = set()
    for j in range(j_min, j_max + 1):
        d = round(10 ** (j / points_per_decade))
        if d < min_s or d > max_s:
            continue
        if d not in seen:
            seen.add(d)
            durations.append(d)
    if not durations:
        return [min_s]
    return sorted(durations)


def best_effort(values: Sequence[float], duration_s: int) -> float | None:
    """Best rolling average of length ``duration_s`` over ``values``.

    ``None`` if ``duration_s > len(values)`` or ``duration_s <= 0`` (unattainable
    duration for activity—never 0, never truncated value).
    """
    n = len(values)
    if duration_s <= 0 or duration_s > n:
        return None

    prefix = [0.0] * (n + 1)
    for i, v in enumerate(values):
        prefix[i + 1] = prefix[i] + v

    best = (prefix[duration_s] - prefix[0]) / duration_s
    for i in range(1, n - duration_s + 1):
        window_avg = (prefix[i + duration_s] - prefix[i]) / duration_s
        best = max(window_avg, best)
    return best


def best_effort_spans_pause(
    values: Sequence[float],
    duration_s: int,
    segment_boundaries: Sequence[int],
) -> tuple[float | None, bool]:
    """Like :func:`best_effort`, but flag if best window crosses a pause excision.

    ``segment_boundaries``: indices (0-based, ascending) in ``values`` where pause
    excision occurred—i.e., sample at this index is non-contiguous in real time from
    the preceding one (see :class:`t0.compute.regularization.RegularizedSeries`,
    spliced after excision). A winning window spanning such an index mixes non-contiguous
    segments: ``spans_pause`` is then ``True`` (§8.4), excluding value from inter-activity
    records without preventing storage.
    """
    n = len(values)
    if duration_s <= 0 or duration_s > n:
        return None, False

    prefix = [0.0] * (n + 1)
    for i, v in enumerate(values):
        prefix[i + 1] = prefix[i] + v

    boundaries = set(segment_boundaries)

    def window_spans(start: int) -> bool:
        return any(start < b < start + duration_s for b in boundaries)

    best_i = 0
    best = (prefix[duration_s] - prefix[0]) / duration_s
    for i in range(1, n - duration_s + 1):
        window_avg = (prefix[i + duration_s] - prefix[i]) / duration_s
        if window_avg > best:
            best = window_avg
            best_i = i
    return best, window_spans(best_i)


def power_duration_curve(
    values: Sequence[float],
    *,
    min_s: int = 1,
    max_s: int | None = None,
    points_per_decade: int = DEFAULT_POINTS_PER_DECADE,
) -> dict[int, float | None]:
    """Complete curve ``{duration_s: best_effort}`` on log-spaced grid (§8.2-8.3).

    ``max_s`` defaults to ``len(values)`` (no unattainable durations generated
    unnecessarily). Durations above ``len(values)`` never included anyway.
    """
    n = len(values)
    upper = max_s if max_s is not None else max(n, min_s)
    durations = log_spaced_durations(min_s, upper, points_per_decade=points_per_decade)
    return {d: best_effort(values, d) for d in durations}
