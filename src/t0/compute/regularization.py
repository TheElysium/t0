"""Series regularization at 1 Hz: gaps, imputation, pause excision.

Mandatory step before any series metric (NP, power-duration curve, NGP...)—see
``04-load-formulas.md`` §1.3. Deliberately **separated** from algorithms consuming
regularized series (:mod:`t0.compute.normalized_power` etc.): those thus have
no gap assumptions, and test cases ("algorithm mechanics", e.g. ``p = [100, 200, 300, 400]``)
transcribe directly from spec without regularization.

Pause convention: intermediate structure (:mod:`t0.fit.model`) carries no FIT
``event`` stream (stop/start). Caller with knowledge of events provides them explicitly
via ``pause_intervals``; absent those, any gap > ``max_interp_gap_s`` seconds is treated
as sensor loss (filled with ``fill_value``), never assumed pause.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence


@dataclass(frozen=True, slots=True)
class Gap:
    """Sampling gap: seconds ``[start_s, end_s]`` missing (both bounds inclusive)."""

    start_s: int
    end_s: int

    @property
    def length_s(self) -> int:
        """Missing seconds in this gap."""
        return self.end_s - self.start_s + 1


@dataclass(frozen=True, slots=True)
class RegularizedSeries:
    """Series regularized at 1 Hz, pauses excised (§1.3).

    ``values`` and ``imputed`` share length ``total_seconds``: timer-time after pause
    excision (potentially less than wall-clock seconds between first and last raw sample).

    ``segment_boundaries``: **indices in ``values``** of first sample of each segment
    spliced after excision (never 0; empty if no pauses excised): sample at this index
    is non-contiguous in real time from its predecessor. Exactly the argument expected by
    :func:`t0.compute.power_curve.best_effort_spans_pause`—without this field,
    caller has no way to locate splices afterward.
    """

    values: tuple[float, ...]
    imputed: tuple[bool, ...]
    imputed_seconds: int
    excised_seconds: int
    total_seconds: int
    data_quality: str  # "good" | "poor" | "unknown" (empty series)
    segment_boundaries: tuple[int, ...] = ()


def detect_gaps(elapsed_s: Sequence[int]) -> list[Gap]:
    """Detect sampling gaps in strictly ascending timestamp sequence.

    ``elapsed_s``: elapsed time (integer seconds) of *present* samples, already sorted.
    Raises ``ValueError`` if not strictly ascending—duplicate or backward time is
    caller error, not a gap.
    """
    gaps: list[Gap] = []
    for prev, curr in pairwise(elapsed_s):
        if curr <= prev:
            msg = "elapsed_s must be strictly increasing"
            raise ValueError(msg)
        if curr - prev > 1:
            gaps.append(Gap(start_s=prev + 1, end_s=curr - 1))
    return gaps


def _interpolated(prev_v: float, curr_v: float, gap: int) -> tuple[list[float], list[bool]]:
    """Linear interpolation of a short gap (§1.3 point 1): ``gap`` samples."""
    return (
        [prev_v + (curr_v - prev_v) * (k / (gap + 1)) for k in range(1, gap + 1)],
        [True] * gap,
    )


def _long_gap(
    first_missing: int,
    last_missing: int,
    *,
    pause_intervals: Collection[tuple[int, int]],
    fill_value: float,
) -> tuple[list[float], list[bool], bool]:
    """Samples for a long gap: none + ``True`` flag if fully paused (§1.3 point 2),
    else ``fill_value`` imputation (point 3)."""
    if any(start <= first_missing and last_missing <= end for start, end in pause_intervals):
        return [], [], True
    gap = last_missing - first_missing + 1
    return [fill_value] * gap, [True] * gap, False


def regularize_series(
    elapsed_s: Sequence[int],
    values: Sequence[float],
    *,
    pause_intervals: Collection[tuple[int, int]] = (),
    fill_value: float = 0.0,
    max_interp_gap_s: int = 3,
    poor_quality_threshold: float = 0.10,
) -> RegularizedSeries:
    """Regularize irregular series to dense 1 Hz grid.

    Mandatory procedure (§1.3, in order):

    1. Gap ``<= max_interp_gap_s`` seconds: linear interpolation between bounding samples.
    2. Longer gap, **fully** covered by ``pause_intervals`` entry (inclusive bounds in
       ``elapsed_s`` seconds): excised—no seconds produced, segments spliced.
    3. Longer gap, outside pause: filled with ``fill_value`` (0 W default for power—
       conservative imputation, §1.3 point 4) and marked ``imputed``.

    Each excision (case 2) records splice index in ``RegularizedSeries.segment_boundaries``,
    passed directly to :func:`t0.compute.power_curve.best_effort_spans_pause`.

    ``elapsed_s`` and ``values`` must be same length and ``elapsed_s`` strictly ascending
    (``ValueError`` else—programming error, not data gap). Empty input → empty
    ``RegularizedSeries``, ``data_quality="unknown"``.
    """
    if len(elapsed_s) != len(values):
        msg = "elapsed_s and values must have the same length"
        raise ValueError(msg)
    if not elapsed_s:
        return RegularizedSeries((), (), 0, 0, 0, "unknown", ())

    out_values: list[float] = [values[0]]
    out_imputed: list[bool] = [False]
    imputed_seconds = 0
    excised_seconds = 0
    segment_boundaries: list[int] = []

    pairs = zip(
        zip(elapsed_s, values, strict=True),
        zip(elapsed_s[1:], values[1:], strict=True),
        strict=False,
    )
    for (prev_t, prev_v), (curr_t, curr_v) in pairs:
        if curr_t <= prev_t:
            msg = "elapsed_s must be strictly increasing"
            raise ValueError(msg)
        gap = curr_t - prev_t - 1
        if 0 < gap <= max_interp_gap_s:
            samples, imputed = _interpolated(prev_v, curr_v, gap)
            out_values.extend(samples)
            out_imputed.extend(imputed)
            imputed_seconds += gap
        elif gap > max_interp_gap_s:
            samples, imputed, paused = _long_gap(
                prev_t + 1, curr_t - 1, pause_intervals=pause_intervals, fill_value=fill_value
            )
            if paused:
                excised_seconds += gap
                # curr_v added next: output index is first of spliced segment.
                segment_boundaries.append(len(out_values))
            else:
                out_values.extend(samples)
                out_imputed.extend(imputed)
                imputed_seconds += gap
        out_values.append(curr_v)
        out_imputed.append(False)

    total_seconds = len(out_values)
    quality = "good"
    if total_seconds > 0 and imputed_seconds / total_seconds > poor_quality_threshold:
        quality = "poor"

    return RegularizedSeries(
        values=tuple(out_values),
        imputed=tuple(out_imputed),
        imputed_seconds=imputed_seconds,
        excised_seconds=excised_seconds,
        total_seconds=total_seconds,
        data_quality=quality,
        segment_boundaries=tuple(segment_boundaries),
    )
