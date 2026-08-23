"""Heart rate histogram at 1-bpm resolution (section 9, core.activity_hr_histogram).

Deliberately not pre-aggregated into zones: zones depend on athlete threshold
(LTHR) that can change retroactively (see addendum section 5, LTHR reported later).
Storing at 1-bpm granularity lets zones be recalculated in SQL without re-ingestion—
the macro plan principle: immutable in Python, mutable in SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Physiological plausibility bounds, aligned with SQL constraint
#: CHECK (hr_bpm BETWEEN 20 AND 260) in core.activity_hr_histogram.
MIN_PLAUSIBLE_BPM = 20
MAX_PLAUSIBLE_BPM = 260


def hr_histogram(heart_rate_bpm: Sequence[int | None]) -> dict[int, int]:
    """Build histogram {bpm: seconds} from 1-Hz heart rate sequence.

    None samples (missing HR, sensor disconnected) are skipped—never count as
    0 bpm. Out-of-plausible-range values ([20, 260]) are also skipped: sensor
    artifacts, not real beats. Including them would corrupt all downstream zones.
    """
    histogram: dict[int, int] = {}
    for bpm in heart_rate_bpm:
        if bpm is None:
            continue
        if bpm < MIN_PLAUSIBLE_BPM or bpm > MAX_PLAUSIBLE_BPM:
            continue
        histogram[bpm] = histogram.get(bpm, 0) + 1
    return histogram
