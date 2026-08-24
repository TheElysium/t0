"""Where ``core.activity.start_time`` comes from, and what happens when the file is silent.

The column is a ``timestamptz``. PostgreSQL accepts a naive datetime without complaint:
it interprets it as UTC. A session shifted two hours doesn't violate any constraint,
raise any error, and silently corrupts all daily PMC grouping. This file monitors
that one flaw.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from garminator.fit.model import ParsedActivitySource
from garminator.transform.activity import ActivityContext, build_core_rows

from .conftest import ALL_THRESHOLD, ATHLETE_ID, RUN_THRESHOLD, make_sample

#: Naive local time as ``build_activity_context`` produces it from ``startTimeLocal``,
#: and offset inferred from the gap with ``startTimeGMT`` (09:00 local - 07:00 GMT = +2 h).
LOCAL_NAIF = datetime(2026, 6, 1, 9, 0)
OFFSET_S = 7200

CONTEXT = ActivityContext(
    activity_id=1,
    sport_code="running",
    discipline="run",
    duration_s=3600,
    start_time_local=LOCAL_NAIF,
    tz_offset_s=OFFSET_S,
)


def _decoded(*, start_time: datetime | None) -> ParsedActivitySource:
    samples = [make_sample(i, heart_rate=150, speed_mps=4.2) for i in range(0, 3600, 60)]
    if start_time is not None:
        samples = [replace(s, t=start_time + timedelta(seconds=s.elapsed_s)) for s in samples]
    return ParsedActivitySource(
        source_format="fit",
        samples=samples,
        laps=[],
        swim_lengths=[],
        session=None,
        start_time=start_time,
    )


def _build(decoded: ParsedActivitySource) -> datetime:
    bundle = build_core_rows(
        decoded,
        CONTEXT,
        athlete_id=ATHLETE_ID,
        fit_file_id=None,
        threshold=RUN_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )
    return bundle.activity.start_time


def test_file_timestamp_takes_precedence_over_summary() -> None:
    """File carries the device timestamp; summary has minute-level precision only.

    The two sources intentionally diverge by 37 seconds: without this gap, the test
    would pass regardless of which source wins, and prove nothing.
    """
    file_instant = datetime(2026, 6, 1, 7, 0, 37, tzinfo=UTC)
    assert _build(_decoded(start_time=file_instant)) == file_instant


def test_without_timestamp_silent_file_adjusted_by_summary_offset() -> None:
    """Reachable fallback: TCX without trackpoint timestamps gives no time.

    Writing ``start_time_local`` as-is would shift the session 2 hours into the past
    without any constraint complaining. We verify both the absolute instant AND that it
    carries a timezone -- a naive one would be read back as 09:00 UTC.
    """
    obtenu = _build(_decoded(start_time=None))

    assert obtenu.tzinfo is not None
    assert obtenu == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    assert obtenu != datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    assert obtenu.utcoffset() == timezone(timedelta(seconds=OFFSET_S)).utcoffset(None)
