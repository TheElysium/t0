"""Orchestration of the ``raw.api_response`` -> ``core.*`` wellness pass.

THE GAP THIS CLOSES. :mod:`t0.transform.runner` walks ``raw.fit_file``: it turns
archived files into activities, and it is the only transform the project had. The wellness
endpoints archive no file -- their entire content is the JSON payload already sitting in
``raw.api_response`` -- so ``core.daily_wellness``, ``core.sleep_session``,
``core.sleep_stage`` and ``core.wellness_sample`` stayed empty after a fully successful
poll, and the wellness dashboard showed nothing with no error anywhere to explain it.

UNIT OF WORK: THE LOCAL DAY, NOT THE PAYLOAD. A dozen endpoints each contribute a few columns
of the SAME ``core.daily_wellness`` row. Writing per payload would mean one upsert per payload per
day, each having to preserve the columns the others own -- one forgotten column and a
later endpoint silently blanks an earlier one. Merging in memory first, then writing once
per day, makes that class of bug unrepresentable.

INCREMENTALITY IS DRIVEN BY ``source_sha256``, NOT BY A WATERMARK. The pass compares the
digest of the payloads present for a day against ``core.daily_wellness.source_sha256`` and
skips the day when they match. A watermark ("everything since the last run") would be wrong
in both directions here: the scheduler re-polls a lookback window every night, so most days
have new payload rows with IDENTICAL content, while a night Garmin recomputes days later
must be reprocessed even though its date is old. The digest answers the only question that
matters -- did the CONTENT change -- and ``--force`` exists for the case where the extraction
code itself changed, which no digest can detect.

A DAY THAT FAILS DOES NOT STOP THE PASS. Each day is written in its own transaction; a day
raising is logged, counted, and the pass moves on. The alternative -- aborting the batch --
would let one malformed payload from 2024 keep last night's sleep out of the dashboard
indefinitely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from t0.transform.wellness import (
    METRIC_ID_BODY_BATTERY,
    METRIC_ID_HRV,
    METRIC_ID_STRESS,
    DailyWellnessRow,
    SleepSessionRow,
    SleepStageRow,
    WellnessSampleRow,
    extract_body_battery,
    extract_floors,
    extract_hrv,
    extract_intensity_minutes,
    extract_resting_hr,
    extract_sleep,
    extract_steps,
    extract_stress,
    extract_training_readiness,
    extract_vo2max,
    extract_weight_kg,
)
from t0.transform.wellness_store import combine_source_hashes, store_wellness_day

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import date

logger = logging.getLogger(__name__)

#: Endpoints this pass reads, in ``raw.api_response.endpoint`` spelling. ``get_activities_by_date``
#: is deliberately ABSENT: it feeds activities, which :mod:`t0.transform.runner` owns.
WELLNESS_ENDPOINTS: tuple[str, ...] = (
    "get_sleep_data",
    "get_hrv_data",
    "get_rhr_day",
    "get_stress_data",
    "get_body_battery",
    "get_steps_data",
    "get_floors",
    "get_intensity_minutes_data",
    "get_training_status",
    "get_max_metrics",
    "get_body_composition",
    "get_training_readiness",
)

#: Metrics this pass owns in ``core.wellness_sample``. Passed to the store as the DELETE
#: scope, so a day whose series came back empty loses the previous pass's points instead of
#: keeping stale ones -- see ``wellness_store._replace_samples``.
OWNED_METRIC_IDS: tuple[int, ...] = (METRIC_ID_STRESS, METRIC_ID_BODY_BATTERY, METRIC_ID_HRV)

#: Days handled per pass. Same reasoning as ``runner.DEFAULT_BATCH_LIMIT``: several short
#: passes beat one long pass that a pod eviction resets to zero. A year of backfill is
#: roughly two passes at this size.
DEFAULT_DAY_LIMIT = 400

#: Progress is logged every N days: a 400-day backfill must not look hung.
PROGRESS_LOG_EVERY_N_DAYS = 50


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def fetchall(self) -> list[Any]: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Minimal structural view of a psycopg 3 connection, for test injection."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """What became of one local day."""

    local_date: date
    status: str  # "stored" | "unchanged" | "failed"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class WellnessReport:
    """Summary of one pass."""

    outcomes: tuple[DayOutcome, ...]
    samples_written: int = 0
    sleep_nights_written: int = 0

    @property
    def stored(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "stored")

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "unchanged")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


@dataclass(frozen=True, slots=True)
class DayPayloads:
    """Every wellness payload archived for one local day, latest version per endpoint."""

    local_date: date
    payloads: Mapping[str, Any]
    hashes: Mapping[str, bytes | None]


# -- reading raw.api_response ------------------------------------------------------------


def select_candidate_days(
    conn: DBConnection, *, since: date | None = None, limit: int = DEFAULT_DAY_LIMIT
) -> list[date]:
    """Local days holding at least one wellness payload, OLDEST FIRST.

    Oldest first so an interrupted backfill resumes where it stopped instead of endlessly
    reprocessing recent days; ``scope_date`` is populated on every row (verified across all
    endpoints), so no fallback to ``fetched_at`` is needed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT scope_date
            FROM raw.api_response
            WHERE endpoint = ANY(%s)
              AND scope_date IS NOT NULL
              AND (%s::date IS NULL OR scope_date >= %s::date)
            ORDER BY scope_date
            LIMIT %s
            """,
            (list(WELLNESS_ENDPOINTS), since, since, limit),
        )
        return [row[0] for row in cur.fetchall()]


def load_day_digests(conn: DBConnection, days: Sequence[date]) -> dict[date, bytes | None]:
    """Combined content digest per day, WITHOUT reading the payloads themselves.

    This is what makes the pass cheap on a nightly run: deciding whether a day changed only
    needs ``payload_sha256`` (32 bytes per row), not the megabytes of JSON behind it.
    """
    if not days:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (scope_date, endpoint) scope_date, endpoint, payload_sha256
            FROM raw.api_response
            WHERE endpoint = ANY(%s) AND scope_date = ANY(%s)
            ORDER BY scope_date, endpoint, fetched_at DESC, id DESC
            """,
            (list(WELLNESS_ENDPOINTS), list(days)),
        )
        rows = cur.fetchall()
    per_day: dict[date, list[bytes | None]] = {}
    for scope_date, _endpoint, payload_sha256 in rows:
        per_day.setdefault(scope_date, []).append(payload_sha256)
    return {day: combine_source_hashes(hashes) for day, hashes in per_day.items()}


def load_stored_digests(
    conn: DBConnection, *, athlete_id: int, days: Sequence[date]
) -> dict[date, bytes | None]:
    """``core.daily_wellness.source_sha256`` already stored for these days."""
    if not days:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT local_date, source_sha256
            FROM core.daily_wellness
            WHERE athlete_id = %s AND local_date = ANY(%s)
            """,
            (athlete_id, list(days)),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def load_day_payloads(conn: DBConnection, day: date) -> DayPayloads:
    """Latest payload per endpoint for one day.

    ``DISTINCT ON`` with ``fetched_at DESC, id DESC`` picks the most recent version: Garmin
    republishes a day (recomputed sleep, late-syncing device), and the older row must lose.
    ``id`` breaks ties because a catch-up pass can archive several windows within the same
    ``fetched_at`` clock tick.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (endpoint) endpoint, payload, payload_sha256
            FROM raw.api_response
            WHERE endpoint = ANY(%s) AND scope_date = %s
            ORDER BY endpoint, fetched_at DESC, id DESC
            """,
            (list(WELLNESS_ENDPOINTS), day),
        )
        rows = cur.fetchall()
    return DayPayloads(
        local_date=day,
        payloads={endpoint: payload for endpoint, payload, _ in rows},
        hashes={endpoint: payload_sha256 for endpoint, _, payload_sha256 in rows},
    )


# -- merging one day ---------------------------------------------------------------------


def _as_mapping(payload: Any) -> dict[str, Any] | None:
    """Accept the dict-or-single-element-list shape Garmin uses inconsistently.

    ``get_max_metrics`` answers with a one-element LIST wrapping the same object
    ``get_training_status`` returns bare. Normalizing here keeps every extraction function
    in :mod:`t0.transform.wellness` typed on a plain mapping.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    return None


@dataclass(frozen=True, slots=True)
class MergedDay:
    """One day's extraction result, ready to hand to the store."""

    daily: DailyWellnessRow
    source_sha256: bytes | None
    sleep: SleepSessionRow | None
    stages: tuple[SleepStageRow, ...]
    samples: tuple[WellnessSampleRow, ...]


#: The three whole-day counters, each an endpoint contributing exactly one scalar column of
#: ``core.daily_wellness`` through an extractor that needs nothing but its payload. Kept as a
#: table rather than three near-identical blocks: adding a fourth counter is then one line.
#: The column is set by an explicit ``replace`` rather than by a column name resolved at
#: runtime, so a typo is a type error here instead of a silently unwritten column.
_DAILY_COUNTERS: tuple[tuple[str, Callable[[DailyWellnessRow, Any], DailyWellnessRow]], ...] = (
    ("get_steps_data", lambda row, payload: replace(row, steps=extract_steps(payload))),
    ("get_floors", lambda row, payload: replace(row, floors=extract_floors(payload))),
    (
        "get_intensity_minutes_data",
        lambda row, payload: replace(row, intensity_minutes=extract_intensity_minutes(payload)),
    ),
)


def _merge_sleep(
    day: DayPayloads, *, athlete_id: int
) -> tuple[SleepSessionRow | None, tuple[SleepStageRow, ...]]:
    """Sleep session + stages, or ``(None, ())`` when the endpoint answered nothing."""
    payload = _as_mapping(day.payloads.get("get_sleep_data"))
    if payload is None:
        return None, ()
    sleep, extracted_stages = extract_sleep(
        payload, athlete_id=athlete_id, source_sha256=day.hashes.get("get_sleep_data")
    )
    return sleep, tuple(extracted_stages)


def _merge_vo2max(daily: DailyWellnessRow, day: DayPayloads) -> DailyWellnessRow:
    """VO2max: get_training_status preferred, get_max_metrics fallback. Both carry the same
    ``mostRecentVO2Max`` block; the first yielding a value wins, so an endpoint that happened
    to answer empty that day never blanks a known estimate."""
    for endpoint in ("get_training_status", "get_max_metrics"):
        payload = _as_mapping(day.payloads.get(endpoint))
        if payload is None:
            continue
        running, cycling = extract_vo2max(payload)
        if running is not None or cycling is not None:
            return replace(daily, vo2max_running=running, vo2max_cycling=cycling)
    return daily


def merge_day(day: DayPayloads, *, athlete_id: int) -> MergedDay:
    """Turn one day's payloads into the rows to write. PURE: no connection, no clock.

    Every endpoint is optional. A day polled before an endpoint was added to the catalog,
    or a day Garmin answered ``[]`` for, simply leaves its columns ``NULL`` -- which is what
    ``core.daily_wellness`` nullability is for, and never a zero.
    """
    daily = DailyWellnessRow(athlete_id=athlete_id, local_date=day.local_date)
    samples: list[WellnessSampleRow] = []

    sleep, stages = _merge_sleep(day, athlete_id=athlete_id)

    hrv_payload = _as_mapping(day.payloads.get("get_hrv_data"))
    if hrv_payload is not None:
        last_night_avg, status, hrv_samples = extract_hrv(
            hrv_payload, athlete_id=athlete_id, local_date=day.local_date
        )
        daily = replace(daily, hrv_last_night_avg=last_night_avg, hrv_status=status)
        samples.extend(hrv_samples)

    rhr_payload = _as_mapping(day.payloads.get("get_rhr_day"))
    if rhr_payload is not None:
        daily = replace(
            daily, resting_hr=extract_resting_hr(rhr_payload, local_date=day.local_date)
        )

    stress_payload = _as_mapping(day.payloads.get("get_stress_data"))
    if stress_payload is not None:
        avg_stress, stress_samples = extract_stress(
            stress_payload, athlete_id=athlete_id, local_date=day.local_date
        )
        daily = replace(daily, avg_stress=avg_stress)
        samples.extend(stress_samples)

    if "get_body_battery" in day.payloads:
        high, low, bb_samples = extract_body_battery(
            day.payloads["get_body_battery"], athlete_id=athlete_id, local_date=day.local_date
        )
        daily = replace(daily, body_battery_high=high, body_battery_low=low)
        samples.extend(bb_samples)

    for endpoint, apply_counter in _DAILY_COUNTERS:
        counter_payload = day.payloads.get(endpoint)
        if counter_payload is not None:
            daily = apply_counter(daily, counter_payload)

    daily = _merge_vo2max(daily, day)

    composition_payload = _as_mapping(day.payloads.get("get_body_composition"))
    if composition_payload is not None:
        daily = replace(
            daily, weight_kg=extract_weight_kg(composition_payload, local_date=day.local_date)
        )

    if "get_training_readiness" in day.payloads:
        daily = replace(
            daily,
            training_readiness=extract_training_readiness(day.payloads["get_training_readiness"]),
        )

    return MergedDay(
        daily=daily,
        source_sha256=combine_source_hashes(day.hashes.values()),
        sleep=sleep,
        stages=stages,
        samples=tuple(samples),
    )


# -- the pass ------------------------------------------------------------------------------


def transform_wellness_days(
    conn: DBConnection,
    *,
    athlete_id: int,
    since: date | None = None,
    limit: int = DEFAULT_DAY_LIMIT,
    force: bool = False,
) -> WellnessReport:
    """Process every changed day and return the pass summary."""
    days = select_candidate_days(conn, since=since, limit=limit)
    if not days:
        logger.info("No wellness payload found in raw.api_response: nothing to transform.")
        return WellnessReport(outcomes=())

    if force:
        todo = list(days)
    else:
        current = load_day_digests(conn, days)
        stored = load_stored_digests(conn, athlete_id=athlete_id, days=days)
        todo = [day for day in days if day not in stored or stored[day] != current.get(day)]

    logger.info(
        "Wellness: %d day(s) with payloads, %d to (re)process, %s to %s.",
        len(days),
        len(todo),
        days[0],
        days[-1],
    )

    outcomes: list[DayOutcome] = [
        DayOutcome(local_date=day, status="unchanged") for day in days if day not in set(todo)
    ]
    samples_written = 0
    nights_written = 0

    for index, day in enumerate(todo, start=1):
        try:
            merged = merge_day(load_day_payloads(conn, day), athlete_id=athlete_id)
            result = store_wellness_day(
                conn,
                daily=merged.daily,
                source_sha256=merged.source_sha256,
                sleep=merged.sleep,
                stages=merged.stages,
                samples=merged.samples,
                sample_metric_ids=OWNED_METRIC_IDS,
            )
        except Exception as exc:  # one bad day must not abort the whole batch
            conn.rollback()
            logger.warning("Wellness day %s failed (%s): skipped, replayable.", day, exc)
            outcomes.append(DayOutcome(local_date=day, status="failed", detail=str(exc)))
        else:
            samples_written += result.samples
            nights_written += result.sleep_sessions
            outcomes.append(DayOutcome(local_date=day, status="stored"))
        if index % PROGRESS_LOG_EVERY_N_DAYS == 0:
            logger.info("Wellness: %s/%s day(s) processed.", index, len(todo))

    outcomes.sort(key=lambda outcome: outcome.local_date)
    return WellnessReport(
        outcomes=tuple(outcomes),
        samples_written=samples_written,
        sleep_nights_written=nights_written,
    )


def format_wellness_report(report: WellnessReport) -> str:
    """Readable report to stdout, in line with the other entry points."""
    lines = [
        f"Wellness: {report.stored} day(s) stored, {report.unchanged} unchanged, "
        f"{report.failed} failed; {report.sleep_nights_written} night(s), "
        f"{report.samples_written} intraday sample(s)."
    ]
    lines.extend(
        f"  {o.local_date} -- {o.status}: {o.detail}"
        for o in report.outcomes
        if o.status == "failed"
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_DAY_LIMIT",
    "OWNED_METRIC_IDS",
    "WELLNESS_ENDPOINTS",
    "DBConnection",
    "DayOutcome",
    "DayPayloads",
    "MergedDay",
    "WellnessReport",
    "format_wellness_report",
    "load_day_digests",
    "load_day_payloads",
    "load_stored_digests",
    "merge_day",
    "select_candidate_days",
    "transform_wellness_days",
]
