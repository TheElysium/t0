"""Idempotent write of a ``CoreActivityBundle`` to ``core.*`` tables.

Pattern taken WITHOUT DEVIATION from ``t0.garmin.raw_store`` (see its module
docstring for full justification): functions below accept a connection typed by
structural protocol :class:`DBConnection`, never concrete ``psycopg.Connection`` --
``t0.db`` exposes no generic query utilities, and a structural Protocol lets
tests inject a fake in-memory connection (no PostgreSQL here) while staying mypy-verified,
since ``psycopg.Connection`` satisfies it structurally. This module opens no connection
itself and imports ``psycopg`` only at type level (``Jsonb``, used for ``jsonb`` columns).

IDEMPOTENCE STRATEGY, deliberately chosen differently per table:

- ``core.activity`` (one row per activity, with columns that host human post-hoc
  decisions -- ``needs_manual_rpe``, future edited ``name``) uses ``INSERT ... ON CONFLICT
  (activity_id) DO UPDATE``, preserving ``ingested_at`` (``DEFAULT now()``, never rewritten)
  and forcing ``updated_at = now()`` explicitly each pass.
- All series tables (``core.sample``, ``core.activity_lap``, ``core.swim_length``,
  ``core.activity_hr_histogram``, ``core.activity_power_curve``, ``core.activity_metric``)
  use full replacement per activity: ``DELETE ... WHERE activity_id = %s`` then bulk
  reinsertion. It is the only honest strategy here, for two asymmetric reasons:

  1. A generic ``ON CONFLICT DO UPDATE`` by primary key NEVER removes a line that became
     orphaned: if a fixed decoder produces fewer samples or fewer laps than before (a bug
     fix that reglues two laps, for example), excess old lines stay in the database
     forever, invisible in the bundle diff but real in ``mart`` views.
  2. ``core.activity_metric`` carries a PARTIAL unique index
     ``activity_metric_canonical_uq ON (activity_id, metric) WHERE is_canonical`` --
     distinct from its primary key ``(activity_id, metric, method_code)``. An ``ON CONFLICT``
     targeting the PK does NOT protect against this second index: if a metric's canonical
     method changes between passes (e.g., hrTSS becomes power TSS because a sensor was added),
     the new canonical row and the old one (which the PK does not consider in conflict
     because ``method_code`` differs) would together violate the partial index. Prior DELETE
     is the only safe path that assumes nothing about canonical-method stability pass-to-pass.

The ENTIRE bundle writes in ONE transaction (single ``conn.commit()``, at the end):
``core.sample`` carries ``ON DELETE CASCADE`` to ``core.activity``, so the ``core.activity``
row must exist before its series are inserted -- but the reverse matters equally. A
half-written activity (samples without load metrics, or vice versa) would be worse than no
activity: it becomes visible in ``core.activity`` -- thus potentially in a JOIN -- while
incomplete in ``mart`` views dependent on tables not yet written, with no error to flag it.
If a step fails, absence of ``commit()`` lets PostgreSQL implicitly cancel the open
transaction on next connection (or explicit ``rollback()`` by the caller) -- nothing partial
is ever visible.

``NULL`` is never recoded to 0 anywhere in this module: parameters passed to
``execute``/``executemany`` are tuples built by simple projection from dataclasses in
``t0.transform.rows`` / ``t0.fit.model``, never retouched. This is the
central rule for ``core.activity_metric.value`` (migrations/005_core_series.sql) -- a
``trimp``/``hrtss`` not computable (no HR, power sensor only) must stay ``NULL``, never
become zero that would artificially depress a load average.

``CHECK`` constraints in DDL (HR 20-260, lat/lon bounded, power >= 0, ``seconds > 0`` on
histogram, ``kind IN (...)`` on power curve) are NOT duplicated here: PostgreSQL verifies
them at write time, and duplicating in Python would create a second source of truth that
might diverge from DDL on the first migration change. This module filters nor clips no
value before insertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from collections.abc import Sequence

    from t0.fit.model import LapRow, SampleRow, SwimLengthRow
    from t0.transform.rows import (
        ActivityHrHistogramRow,
        ActivityMetricRow,
        ActivityPowerCurveRow,
        ActivityRow,
        CoreActivityBundle,
    )


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Minimal structural view of a psycopg 3 connection for test injection."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def _jsonb_or_none(extra: dict[str, Any]) -> Jsonb | None:
    """``{}`` -> ``NULL`` rather than an empty JSON object: ``extra`` exists only to
    lose no uncolumned FIT fields -- an empty dict has NO fields to preserve, writing
    it would add visual noise identical across thousands of ``core.sample`` rows without
    ever adding anything exploitable.
    """
    return Jsonb(extra) if extra else None


@dataclass(frozen=True, slots=True)
class StoreResult:
    """Count of rows written by :func:`store_activity_bundle`, table by table."""

    activity_id: int
    samples: int
    laps: int
    swim_lengths: int
    hr_histogram: int
    power_curve: int
    metrics: int


def _upsert_activity(cur: _Cursor, a: ActivityRow) -> None:
    """``INSERT ... ON CONFLICT (activity_id) DO UPDATE``.

    ``ingested_at`` is deliberately absent from the ``SET`` column list: its column has
    ``DEFAULT now()`` in the database and should only be set on the very first write.
    ``updated_at``, in contrast, is systematically reset to ``now()`` via SQL literal
    expression (not ``EXCLUDED.updated_at``, which does not even exist: ``ActivityRow``
    does not carry that field, cf. its docstring).
    """
    cur.execute(
        """
        INSERT INTO core.activity (
            activity_id, athlete_id, sport_code, garmin_type_key,
            start_time, start_time_local, local_date, tz_offset_s,
            duration_s, elapsed_s, moving_s, distance_m, ascent_m, descent_m,
            avg_hr, max_hr, avg_power, max_power, avg_power_nonzero,
            np_watts, np_reliable, avg_speed_mps, avg_cadence, calories,
            ngp_speed_mps, gap_applied, swim_time_s, swim_time_source,
            total_samples, imputed_seconds, excised_seconds, data_quality,
            parent_activity_id, is_parent, leg_order,
            primary_device_id, fit_file_id, summary_sha256, name, needs_manual_rpe, source
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (activity_id) DO UPDATE SET
            athlete_id = EXCLUDED.athlete_id,
            sport_code = EXCLUDED.sport_code,
            garmin_type_key = EXCLUDED.garmin_type_key,
            start_time = EXCLUDED.start_time,
            start_time_local = EXCLUDED.start_time_local,
            local_date = EXCLUDED.local_date,
            tz_offset_s = EXCLUDED.tz_offset_s,
            duration_s = EXCLUDED.duration_s,
            elapsed_s = EXCLUDED.elapsed_s,
            moving_s = EXCLUDED.moving_s,
            distance_m = EXCLUDED.distance_m,
            ascent_m = EXCLUDED.ascent_m,
            descent_m = EXCLUDED.descent_m,
            avg_hr = EXCLUDED.avg_hr,
            max_hr = EXCLUDED.max_hr,
            avg_power = EXCLUDED.avg_power,
            max_power = EXCLUDED.max_power,
            avg_power_nonzero = EXCLUDED.avg_power_nonzero,
            np_watts = EXCLUDED.np_watts,
            np_reliable = EXCLUDED.np_reliable,
            avg_speed_mps = EXCLUDED.avg_speed_mps,
            avg_cadence = EXCLUDED.avg_cadence,
            calories = EXCLUDED.calories,
            ngp_speed_mps = EXCLUDED.ngp_speed_mps,
            gap_applied = EXCLUDED.gap_applied,
            swim_time_s = EXCLUDED.swim_time_s,
            swim_time_source = EXCLUDED.swim_time_source,
            total_samples = EXCLUDED.total_samples,
            imputed_seconds = EXCLUDED.imputed_seconds,
            excised_seconds = EXCLUDED.excised_seconds,
            data_quality = EXCLUDED.data_quality,
            parent_activity_id = EXCLUDED.parent_activity_id,
            is_parent = EXCLUDED.is_parent,
            leg_order = EXCLUDED.leg_order,
            primary_device_id = EXCLUDED.primary_device_id,
            fit_file_id = EXCLUDED.fit_file_id,
            summary_sha256 = EXCLUDED.summary_sha256,
            name = EXCLUDED.name,
            needs_manual_rpe = EXCLUDED.needs_manual_rpe,
            source = EXCLUDED.source,
            updated_at = now();
        """,
        (
            a.activity_id,
            a.athlete_id,
            a.sport_code,
            a.garmin_type_key,
            a.start_time,
            a.start_time_local,
            a.local_date,
            a.tz_offset_s,
            a.duration_s,
            a.elapsed_s,
            a.moving_s,
            a.distance_m,
            a.ascent_m,
            a.descent_m,
            a.avg_hr,
            a.max_hr,
            a.avg_power,
            a.max_power,
            a.avg_power_nonzero,
            a.np_watts,
            a.np_reliable,
            a.avg_speed_mps,
            a.avg_cadence,
            a.calories,
            a.ngp_speed_mps,
            a.gap_applied,
            a.swim_time_s,
            a.swim_time_source,
            a.total_samples,
            a.imputed_seconds,
            a.excised_seconds,
            a.data_quality,
            a.parent_activity_id,
            a.is_parent,
            a.leg_order,
            a.primary_device_id,
            a.fit_file_id,
            a.summary_sha256,
            a.name,
            a.needs_manual_rpe,
            a.source,
        ),
    )


def _replace_samples(cur: _Cursor, activity_id: int, rows: Sequence[SampleRow]) -> int:
    cur.execute("DELETE FROM core.sample WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    # executemany rather than one execute() per row: a long session produces thousands
    # of core.sample (up to one-way ~2h run, so ~7000 rows). One network round-trip per
    # row would dominate write time vs. a single batch -- this is not micro-optimization,
    # it is the difference between sub-second ingestion and several seconds per activity,
    # multiplied by hundreds of activities on each full replay.
    cur.executemany(
        """
        INSERT INTO core.sample (
            activity_id, t, elapsed_s, latitude_deg, longitude_deg, altitude_m,
            distance_m, speed_mps, heart_rate, cadence, power, temperature,
            vertical_oscillation, stance_time, step_length, vertical_ratio, extra
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                s.activity_id,
                s.t,
                s.elapsed_s,
                s.latitude_deg,
                s.longitude_deg,
                s.altitude_m,
                s.distance_m,
                s.speed_mps,
                s.heart_rate,
                s.cadence,
                s.power,
                s.temperature,
                s.vertical_oscillation,
                s.stance_time,
                s.step_length,
                s.vertical_ratio,
                _jsonb_or_none(s.extra),
            )
            for s in rows
        ],
    )
    return len(rows)


def _replace_laps(cur: _Cursor, activity_id: int, rows: Sequence[LapRow]) -> int:
    cur.execute("DELETE FROM core.activity_lap WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_lap (
            activity_id, lap_index, start_time, duration_s, elapsed_s, distance_m,
            avg_speed_mps, avg_hr, max_hr, avg_power, max_power, avg_cadence,
            ascent_m, descent_m, intensity, lap_trigger, swim_stroke, num_lengths, extra
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                lap.activity_id,
                lap.lap_index,
                lap.start_time,
                lap.duration_s,
                lap.elapsed_s,
                lap.distance_m,
                lap.avg_speed_mps,
                lap.avg_hr,
                lap.max_hr,
                lap.avg_power,
                lap.max_power,
                lap.avg_cadence,
                lap.ascent_m,
                lap.descent_m,
                lap.intensity,
                lap.lap_trigger,
                lap.swim_stroke,
                lap.num_lengths,
                _jsonb_or_none(lap.extra),
            )
            for lap in rows
        ],
    )
    return len(rows)


def _replace_swim_lengths(cur: _Cursor, activity_id: int, rows: Sequence[SwimLengthRow]) -> int:
    cur.execute("DELETE FROM core.swim_length WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.swim_length (
            activity_id, length_index, lap_index, start_time, duration_s,
            length_type, swim_stroke, total_strokes, avg_cadence
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        [
            (
                length.activity_id,
                length.length_index,
                length.lap_index,
                length.start_time,
                length.duration_s,
                length.length_type,
                length.swim_stroke,
                length.total_strokes,
                length.avg_cadence,
            )
            for length in rows
        ],
    )
    return len(rows)


def _replace_hr_histogram(
    cur: _Cursor, activity_id: int, rows: Sequence[ActivityHrHistogramRow]
) -> int:
    cur.execute("DELETE FROM core.activity_hr_histogram WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_hr_histogram (activity_id, hr_bpm, seconds)
        VALUES (%s, %s, %s);
        """,
        [(h.activity_id, h.hr_bpm, h.seconds) for h in rows],
    )
    return len(rows)


def _replace_power_curve(
    cur: _Cursor, activity_id: int, rows: Sequence[ActivityPowerCurveRow]
) -> int:
    cur.execute("DELETE FROM core.activity_power_curve WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_power_curve (
            activity_id, kind, duration_s, value, spans_pause
        )
        VALUES (%s, %s, %s, %s, %s);
        """,
        [(p.activity_id, p.kind, p.duration_s, p.value, p.spans_pause) for p in rows],
    )
    return len(rows)


def _replace_metrics(cur: _Cursor, activity_id: int, rows: Sequence[ActivityMetricRow]) -> int:
    """Full replacement -- see module docstring for the partial unique index.

    A simple ``ON CONFLICT`` on PK ``(activity_id, metric, method_code)`` would not
    protect ``activity_metric_canonical_uq`` (partial, on ``(activity_id, metric) WHERE
    is_canonical``) when canonical method changes between passes: the two rows -- old
    canonical and new -- carry different ``method_code``, so the PK never sees them in
    conflict, and INSERT would violate the partial index. Deleting all activity rows
    first makes this collision structurally impossible regardless of prior method history.
    """
    cur.execute("DELETE FROM core.activity_metric WHERE activity_id = %s;", (activity_id,))
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO core.activity_metric (
            activity_id, metric, method_code, value, is_canonical, threshold_id
        )
        VALUES (%s, %s, %s, %s, %s, %s);
        """,
        [
            (m.activity_id, m.metric, m.method_code, m.value, m.is_canonical, m.threshold_id)
            for m in rows
        ],
    )
    return len(rows)


def store_activity_bundle(conn: DBConnection, bundle: CoreActivityBundle) -> StoreResult:
    """Write the entire ``bundle`` (produced by ``t0.transform.activity.build_core_rows``)
    to the seven ``core.*`` tables it covers, in ONE transaction (single ``commit()`` at
    the end -- see module docstring for what an intermediate commit would break).
    ``core.activity`` is written first: ``core.sample`` carries ``ON DELETE CASCADE`` to
    ``core.activity``, so its rows cannot be inserted before the parent row exists.

    ``core.device`` and ``core.sample_derived`` are not written: ``bundle`` carries no rows
    for them anyway (``bundle.sample_derived`` is always empty -- see ``t0.transform.rows``
    docstring), nothing to populate here.
    """
    with conn.cursor() as cur:
        _upsert_activity(cur, bundle.activity)
        n_samples = _replace_samples(cur, bundle.activity.activity_id, bundle.samples)
        n_laps = _replace_laps(cur, bundle.activity.activity_id, bundle.laps)
        n_swim_lengths = _replace_swim_lengths(
            cur, bundle.activity.activity_id, bundle.swim_lengths
        )
        n_hr_histogram = _replace_hr_histogram(
            cur, bundle.activity.activity_id, bundle.hr_histogram
        )
        n_power_curve = _replace_power_curve(cur, bundle.activity.activity_id, bundle.power_curve)
        n_metrics = _replace_metrics(cur, bundle.activity.activity_id, bundle.metrics)
    conn.commit()

    return StoreResult(
        activity_id=bundle.activity.activity_id,
        samples=n_samples,
        laps=n_laps,
        swim_lengths=n_swim_lengths,
        hr_histogram=n_hr_histogram,
        power_curve=n_power_curve,
        metrics=n_metrics,
    )
