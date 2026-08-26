"""Idempotent write of one day of wellness data to ``core.*``.

Same structural-protocol pattern as :mod:`garminator.transform.store` (see its module
docstring for the full justification): the connection is typed by a ``Protocol``, never as
a concrete ``psycopg.Connection``, so tests can inject an in-memory fake while staying
mypy-verified. This module opens no connection and imports no driver.

IDEMPOTENCE, TABLE BY TABLE -- the choice differs, and deliberately so.

- ``core.daily_wellness`` and ``core.sleep_session`` hold ONE row per key, so
  ``INSERT ... ON CONFLICT DO UPDATE`` is enough: nothing can become orphaned. Neither
  statement lists ``updated_at`` in its ``VALUES``; it is forced to ``now()`` in the
  ``SET`` clause, exactly as ``store.py`` does for ``core.activity``.
- ``core.sleep_stage`` and ``core.wellness_sample`` are SERIES, and are fully replaced for
  the key being written (DELETE then re-insert). ``ON CONFLICT`` on their primary keys
  would update the points still present and leave behind every point that DISAPPEARED --
  and points do disappear here: Garmin republishes a night after recomputing it, often
  with fewer, longer segments. Those ghosts would be invisible in a diff and perfectly
  real in a Grafana panel.

WHY ``core.wellness_sample`` IS DELETED BY ``local_date`` AND ``metric_id``. Its primary
key is ``(metric_id, t)`` and carries no ``athlete_id`` (migration 006: the table is meant
to become a hypertable, hence no FK). The delete is therefore scoped by
``(athlete_id, local_date, metric_id)``, which is exactly the unit a single payload
produces, and served by ``wellness_sample_local_date_idx``. Scoping by ``t`` range instead
would be wrong at both ends: a night's HRV readings belong to a local date whose UTC span
depends on the offset in force that day.

A day writes in ONE transaction (a single ``commit()`` at the very end). A partially
written day -- a sleep session without its phases, an average without the series behind
it -- would show up in the dashboards as real data with holes, and nothing would flag it.

NULL IS NEVER RECODED TO 0 here either. Parameters are plain projections of the
dataclasses from :mod:`garminator.transform.wellness`; a metric the watch did not measure
stays ``NULL``, because 0 is a legitimate value for several of these columns (a Body
Battery of 0 is a real, and bad, reading).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from garminator.transform.wellness import (
        DailyWellnessRow,
        SleepSessionRow,
        SleepStageRow,
        WellnessSampleRow,
    )


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Minimal structural view of a psycopg 3 connection, for test injection."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WellnessStoreResult:
    """Rows written for one day, table by table."""

    daily_wellness: int
    sleep_sessions: int
    sleep_stages: int
    samples: int


def combine_source_hashes(hashes: Iterable[bytes | None]) -> bytes | None:
    """Fold the ``payload_sha256`` of every payload feeding a day into a single digest.

    ``core.daily_wellness.source_sha256`` is documented as "hash of the daily payload that
 produced this row", but a day here is a MERGE of multiple endpoints. Hashing the
    sorted concatenation of their individual digests preserves the property the column
    actually exists for -- detecting that Garmin republished a day with different content,
    without comparing field by field -- while staying deterministic regardless of the order
    the payloads were read in.
    """
    present = sorted(h for h in hashes if h is not None)
    if not present:
        return None
    digest = hashlib.sha256()
    for item in present:
        digest.update(item)
    return digest.digest()


def _upsert_daily_wellness(
    cur: _Cursor, row: DailyWellnessRow, source_sha256: bytes | None
) -> None:
    cur.execute(
        """
        INSERT INTO core.daily_wellness (
            athlete_id, local_date,
            resting_hr, hrv_last_night_avg, hrv_status,
            body_battery_high, body_battery_low, avg_stress,
            steps, floors, intensity_minutes,
            weight_kg, training_readiness, vo2max_running, vo2max_cycling,
            source_sha256
        )
        VALUES (
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s
        )
        ON CONFLICT (athlete_id, local_date) DO UPDATE SET
            resting_hr = EXCLUDED.resting_hr,
            hrv_last_night_avg = EXCLUDED.hrv_last_night_avg,
            hrv_status = EXCLUDED.hrv_status,
            body_battery_high = EXCLUDED.body_battery_high,
            body_battery_low = EXCLUDED.body_battery_low,
            avg_stress = EXCLUDED.avg_stress,
            steps = EXCLUDED.steps,
            floors = EXCLUDED.floors,
            intensity_minutes = EXCLUDED.intensity_minutes,
            weight_kg = EXCLUDED.weight_kg,
            training_readiness = EXCLUDED.training_readiness,
            vo2max_running = EXCLUDED.vo2max_running,
            vo2max_cycling = EXCLUDED.vo2max_cycling,
            source_sha256 = EXCLUDED.source_sha256,
            updated_at = now();
        """,
        (
            row.athlete_id,
            row.local_date,
            row.resting_hr,
            row.hrv_last_night_avg,
            row.hrv_status,
            row.body_battery_high,
            row.body_battery_low,
            row.avg_stress,
            row.steps,
            row.floors,
            row.intensity_minutes,
            row.weight_kg,
            row.training_readiness,
            row.vo2max_running,
            row.vo2max_cycling,
            source_sha256,
        ),
    )


def _upsert_sleep_session(cur: _Cursor, row: SleepSessionRow) -> None:
    cur.execute(
        """
        INSERT INTO core.sleep_session (
            athlete_id, wake_date, sleep_start, sleep_end, sleep_start_local,
            duration_s, deep_s, light_s, rem_s, awake_s,
            sleep_score, avg_spo2, avg_respiration, source_sha256
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (athlete_id, wake_date) DO UPDATE SET
            sleep_start = EXCLUDED.sleep_start,
            sleep_end = EXCLUDED.sleep_end,
            sleep_start_local = EXCLUDED.sleep_start_local,
            duration_s = EXCLUDED.duration_s,
            deep_s = EXCLUDED.deep_s,
            light_s = EXCLUDED.light_s,
            rem_s = EXCLUDED.rem_s,
            awake_s = EXCLUDED.awake_s,
            sleep_score = EXCLUDED.sleep_score,
            avg_spo2 = EXCLUDED.avg_spo2,
            avg_respiration = EXCLUDED.avg_respiration,
            source_sha256 = EXCLUDED.source_sha256,
            updated_at = now();
        """,
        (
            row.athlete_id,
            row.wake_date,
            row.sleep_start,
            row.sleep_end,
            row.sleep_start_local,
            row.duration_s,
            row.deep_s,
            row.light_s,
            row.rem_s,
            row.awake_s,
            row.sleep_score,
            row.avg_spo2,
            row.avg_respiration,
            row.source_sha256,
        ),
    )


def _replace_sleep_stages(
    cur: _Cursor, stages: Sequence[SleepStageRow], session: SleepSessionRow
) -> None:
    """Full replacement of one night's phases.

    The DELETE is issued even when ``stages`` is empty: a night republished WITHOUT its
    ``sleepLevels`` (Garmin does return ``null`` there sometimes) must lose the phases of
    the previous version rather than keep them alongside a summary that no longer matches.
    """
    cur.execute(
        "DELETE FROM core.sleep_stage WHERE athlete_id = %s AND wake_date = %s",
        (session.athlete_id, session.wake_date),
    )
    if not stages:
        return
    cur.executemany(
        """
        INSERT INTO core.sleep_stage (athlete_id, wake_date, stage_start, stage_end, stage)
        VALUES (%s, %s, %s, %s, %s);
        """,
        [(s.athlete_id, s.wake_date, s.stage_start, s.stage_end, s.stage) for s in stages],
    )


def _replace_samples(
    cur: _Cursor,
    samples: Sequence[WellnessSampleRow],
    *,
    athlete_id: int,
    local_date: Any,
    metric_ids: Sequence[int],
) -> None:
    """Full replacement of the given metrics' series for one local day.

    ``metric_ids`` is passed explicitly rather than derived from ``samples``: a day whose
    stress series came back EMPTY this pass must still clear whatever the previous pass
    stored for stress, and deriving the scope from the rows to insert would make exactly
    that case a no-op.
    """
    if metric_ids:
        cur.execute(
            """
            DELETE FROM core.wellness_sample
            WHERE athlete_id = %s AND local_date = %s AND metric_id = ANY(%s)
            """,
            (athlete_id, local_date, list(metric_ids)),
        )
    if not samples:
        return
    cur.executemany(
        """
        INSERT INTO core.wellness_sample (metric_id, t, athlete_id, value, local_date)
        VALUES (%s, %s, %s, %s, %s);
        """,
        [(s.metric_id, s.t, s.athlete_id, s.value, s.local_date) for s in samples],
    )


def store_wellness_day(
    conn: DBConnection,
    *,
    daily: DailyWellnessRow,
    source_sha256: bytes | None = None,
    sleep: SleepSessionRow | None = None,
    stages: Sequence[SleepStageRow] = (),
    samples: Sequence[WellnessSampleRow] = (),
    sample_metric_ids: Sequence[int] = (),
) -> WellnessStoreResult:
    """Write one day of wellness data, in a single transaction.

    ORDER MATTERS: ``core.sleep_stage`` carries a foreign key to ``core.sleep_session``
    (``ON DELETE CASCADE``), so the session must exist before its phases are inserted. When
    no session could be extracted, the phases are dropped rather than written orphaned --
    the database would refuse them anyway.
    """
    with conn.cursor() as cur:
        _upsert_daily_wellness(cur, daily, source_sha256)
        sessions_written = 0
        stages_written = 0
        if sleep is not None:
            _upsert_sleep_session(cur, sleep)
            _replace_sleep_stages(cur, stages, sleep)
            sessions_written = 1
            stages_written = len(stages)
        _replace_samples(
            cur,
            samples,
            athlete_id=daily.athlete_id,
            local_date=daily.local_date,
            metric_ids=sample_metric_ids,
        )
    conn.commit()
    return WellnessStoreResult(
        daily_wellness=1,
        sleep_sessions=sessions_written,
        sleep_stages=stages_written,
        samples=len(samples),
    )


__all__ = [
    "DBConnection",
    "WellnessStoreResult",
    "combine_source_hashes",
    "store_wellness_day",
]
