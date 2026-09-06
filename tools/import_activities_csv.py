"""One-off importer for the Garmin Connect bulk export ``Activities.csv``.

WHY THIS EXISTS, AND WHY IT IS NOT PART OF THE SERVICE
------------------------------------------------------
The service ingests activities through two paths only: the daily poll
(:mod:`t0.scheduler`) and the manual upload endpoint
(:mod:`t0.api.upload`). Neither can reach the historical archive that Garmin
Connect hands out as a ZIP: ``Activities.csv`` is a *summary* export with no activity
identifier, no samples, and localized column headers. This script exists to load that
archive ONCE into a throwaway database, so the schema and the ``mart.*`` views can be
exercised against a real training history before the migrations are frozen by checksum
at first deployment. It is validation tooling, not a service component.

WHAT AN IMPORT FROM THIS FILE CAN AND CANNOT PRODUCE
----------------------------------------------------
The CSV carries per-activity summaries and nothing else -- no time series. Consequently:

* ``core.sample``, ``core.activity_lap``, ``core.swim_length``,
  ``core.activity_hr_histogram`` and ``core.activity_power_curve`` stay EMPTY. This is a
  property of the source file, not a failure.
* ``hr_hrtss`` and ``trimp`` are therefore never computed: both read a heart-rate
  histogram, which only samples can build. ``mart.hr_zone_time`` stays empty too.
* ``bike_power`` is computed from the exported Normalized Power, ``run_gap`` from the
  exported grade-adjusted pace, and ``swim_css`` from distance and moving time. These
  three feed ``mart.activity_load``, ``mart.daily_load`` and ``mart.pmc``, which is the
  point of the exercise.

IDENTIFIERS
-----------
The CSV has no Garmin activity identifier, so every imported row is ``source = 'manual'``
and its ``activity_id`` is derived deterministically from the local start instant:
``_MANUAL_CSV_BASE + int(start_time_local.timestamp())``. Deterministic rather than drawn
from ``core.manual_activity_id_seq`` so that re-running the import UPDATES the same rows
instead of duplicating the whole history. The base sits inside the manual range required
by the CHECK ``activity_synthetic_id_range`` (>= 1e15) but offset well clear of the values
the sequence itself hands out, so a real manual upload can never collide with an imported
row. It also stays below the float64 exact-integer limit (2^53) so the id is not rounded
when read through JSON -- see ``_MANUAL_CSV_BASE`` and migration 024.

UNITS AND ENCODING TRAPS IN THE EXPORT (all verified against the real file)
--------------------------------------------------------------------------
* Headers are localized (French export). Numbers use ``.`` as decimal separator and ``,``
  as thousands separator, so ``1,234`` is one thousand two hundred thirty-four.
* Missing values appear as ``--`` or ``--:--:--``.
* Negative values are prefixed with an Excel text guard apostrophe (``'-12``).
* ``Distance`` is in METRES for swimming activities and in KILOMETRES for every other
  sport.
* ``Allure moyenne`` mixes two units in one column: ``m:ss`` per kilometre for running,
  ``km/h`` for cycling and skiing -- and, for swimming, ``m:ss`` per HUNDRED METRES. The
  swim case is never read here precisely because of that: swim speed is recomputed from
  distance and time.
* ``Cadence de course moyenne`` and ``Cadence de course maximale`` each appear TWICE in
  the header row (the second pair is the cycling cadence). Columns are therefore read by
  POSITION, never through ``csv.DictReader``, which would silently drop one of each pair.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

from t0.load.activity_load import (
    ActivityLoadInput,
    ActivityMetricRow,
    build_activity_metric_rows,
)
from t0.load.thresholds import threshold_for_date
from t0.logging_setup import configure_logging
from t0.transform.rows import ActivityRow, CoreActivityBundle
from t0.transform.store import store_activity_bundle
from t0.transform.summary import DISCIPLINE_BY_SPORT_CODE, load_thresholds

if TYPE_CHECKING:
    from collections.abc import Sequence

    from t0.load.thresholds import Threshold
    from t0.transform.store import DBConnection
    from t0.transform.summary import DBConnection as SummaryDBConnection

#: Base identifier for rows imported from the bulk export. Inside the manual range
#: (>= 1e15, CHECK ``activity_synthetic_id_range``) and sat at 1.5e15 -- the middle of
#: the manual band [1e15, 2e15) -- offset by 5e14 from the start of
#: ``core.manual_activity_id_seq``, which hands out 1e15, 1e15+1, ... : the sequence would
#: need 5e14 manual uploads to reach this band, so an imported row can never collide with
#: a real manual upload. The base MUST stay below the float64 exact-integer limit
#: (2^53 ~ 9.007e15), even after adding a Unix timestamp (~1.7e9): 1.5e15 + 1.7e9 is far
#: under 2e15, so the id survives a JSON round-trip unrounded (MCP, Grafana, browser).
#: This is why the historical 9.1e18 base was abandoned -- see migration 024.
_MANUAL_CSV_BASE = 1_500_000_000_000_000

#: ``Type d'activite`` (localized export value) -> ``core.sport.sport_code``.
#: An unlisted value is never guessed: it raises, naming the observed value. Same
#: non-negotiable rule as :mod:`t0.transform.manual`.
SPORT_CODE_BY_CSV_TYPE: dict[str, str] = {
    "Course à pied": "running",
    "Trail": "trail_running",
    "Tapis de course": "treadmill",
    "Cyclisme": "cycling",
    "Cyclisme virtuel": "virtual_cycling",
    "Vélo d'intérieur": "virtual_cycling",
    "Nat. piscine": "lap_swimming",
    "Natation en eau libre": "open_water",
    "Randonnée": "hiking",
    "Ski en station": "resort_skiing",
    "Multisport": "multisport",
    "Transition": "transition",
    "Musculation": "strength",
}

#: Sports whose ``Distance`` column is expressed in metres rather than kilometres.
_DISTANCE_IN_METRES = frozenset({"lap_swimming", "open_water"})

_MISSING = frozenset({"", "--", "--:--:--", "-"})


class CsvImportError(RuntimeError):
    """A row of the export cannot be turned into a ``core.activity`` row."""


def _clean(value: str | None) -> str | None:
    """Strip the Excel text guard and normalize the export markers for "no value"."""
    if value is None:
        return None
    text = value.strip().lstrip("'").strip()
    return None if text in _MISSING else text


def _num(value: str | None) -> float | None:
    """Parse a number written with ``,`` as thousands separator."""
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    number = _num(value)
    return None if number is None else round(number)


def _hms(value: str | None) -> float | None:
    """Parse ``HH:MM:SS`` or ``HH:MM:SS.s`` into seconds."""
    text = _clean(value)
    if text is None:
        return None
    try:
        numbers = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60.0 + number
    return seconds


def _pace_to_mps(value: str | None) -> float | None:
    """Parse an ``m:ss`` per-kilometre pace into m/s. Returns None for the ``km/h`` form,
    which the caller must handle separately -- the column mixes both units."""
    text = _clean(value)
    if text is None or ":" not in text:
        return None
    seconds = _hms(text)
    if seconds is None or seconds <= 0:
        return None
    return 1000.0 / seconds


@dataclass(frozen=True, slots=True)
class _Columns:
    """Positions of the columns actually read, resolved from the header row.

    Positional because two header names appear twice (see module docstring)."""

    activity_type: int
    date: int
    title: int
    distance: int
    calories: int
    duration: int
    avg_hr: int
    max_hr: int
    avg_pace: int
    ascent: int
    descent: int
    gap_pace: int
    np_watts: int
    avg_power: int
    max_power: int
    moving_time: int
    elapsed_time: int
    avg_run_cadence: int


def _resolve_columns(header: Sequence[str]) -> _Columns:
    def first(name: str) -> int:
        try:
            return list(header).index(name)
        except ValueError as exc:
            msg = (
                f"column {name!r} is absent from the export header. Garmin may have "
                f"changed the export locale or format; observed header: {list(header)}"
            )
            raise CsvImportError(msg) from exc

    return _Columns(
        activity_type=first("Type d'activité"),
        date=first("Date"),
        title=first("Titre"),
        distance=first("Distance"),
        calories=first("Calories"),
        duration=first("Durée"),
        avg_hr=first("Fréquence cardiaque moyenne"),
        max_hr=first("Fréquence cardiaque maximale"),
        avg_pace=first("Allure moyenne"),
        ascent=first("Ascension totale"),
        descent=first("Descente totale"),
        gap_pace=first("GAP moyenne"),
        np_watts=first("Normalized Power® (NP®)"),
        avg_power=first("Puissance moyenne"),
        max_power=first("Puissance max."),
        moving_time=first("Temps de déplacement"),
        elapsed_time=first("Temps écoulé"),
        # First occurrence only: the second one is the cycling cadence.
        avg_run_cadence=first("Cadence de course moyenne"),
    )


def _ngp_speed_mps(
    record: Sequence[str], cols: _Columns, discipline: str
) -> tuple[float | None, bool]:
    """run_gap reads ngp_speed_mps. The export's own grade-adjusted pace is preferred over
    the raw pace; gap_applied records honestly which of the two was used. Neither is read
    outside running: the column holds km/h for cycling and skiing, and min/100 m for
    swimming (see module docstring)."""
    if discipline != "run":
        return None, False
    gap = _pace_to_mps(record[cols.gap_pace])
    if gap is not None:
        return gap, True
    return _pace_to_mps(record[cols.avg_pace]), False


def build_activity_row(
    record: Sequence[str], cols: _Columns, *, athlete_id: int, tz: ZoneInfo, line_no: int
) -> ActivityRow:
    """Turn one export row into a ``core.activity`` row. Raises rather than guessing."""
    csv_type = _clean(record[cols.activity_type])
    if csv_type is None:
        msg = f"line {line_no}: empty activity type."
        raise CsvImportError(msg)
    sport_code = SPORT_CODE_BY_CSV_TYPE.get(csv_type)
    if sport_code is None:
        msg = (
            f"line {line_no}: activity type {csv_type!r} has no entry in "
            f"SPORT_CODE_BY_CSV_TYPE. Add it rather than letting the import guess a "
            f"sport, which would skew the load computed for it."
        )
        raise CsvImportError(msg)

    raw_date = _clean(record[cols.date])
    if raw_date is None:
        msg = f"line {line_no}: empty date."
        raise CsvImportError(msg)
    # Naive by design: the CSV carries no offset; ``tz`` (operator-provided) is attached
    # explicitly right below.
    naive_local = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    aware_local = naive_local.replace(tzinfo=tz)
    offset = aware_local.utcoffset()
    if offset is None:  # pragma: no cover - ZoneInfo always resolves an offset
        msg = f"line {line_no}: timezone {tz} yielded no UTC offset for {naive_local}."
        raise CsvImportError(msg)

    duration_s = _hms(record[cols.duration])
    if duration_s is None or duration_s <= 0:
        msg = f"line {line_no}: unusable duration {record[cols.duration]!r}."
        raise CsvImportError(msg)

    distance_raw = _num(record[cols.distance])
    distance_m: float | None = None
    if distance_raw is not None and distance_raw > 0:
        distance_m = distance_raw if sport_code in _DISTANCE_IN_METRES else distance_raw * 1000.0

    moving_s = _hms(record[cols.moving_time])
    elapsed_s = _hms(record[cols.elapsed_time])

    avg_speed_mps: float | None = None
    if distance_m is not None:
        reference_s = moving_s or duration_s
        if reference_s > 0:
            avg_speed_mps = distance_m / reference_s

    discipline = DISCIPLINE_BY_SPORT_CODE[sport_code]
    ngp_speed_mps, gap_applied = _ngp_speed_mps(record, cols, discipline)

    np_watts = _num(record[cols.np_watts])
    swim_time_s = moving_s if discipline == "swim" else None

    return ActivityRow(
        activity_id=_MANUAL_CSV_BASE + int(aware_local.timestamp()),
        athlete_id=athlete_id,
        sport_code=sport_code,
        garmin_type_key=csv_type,
        start_time=aware_local.astimezone(UTC),
        start_time_local=naive_local,
        local_date=naive_local.date(),
        tz_offset_s=round(offset.total_seconds()),
        duration_s=round(duration_s),
        elapsed_s=None if elapsed_s is None else round(elapsed_s),
        moving_s=None if moving_s is None else round(moving_s),
        distance_m=distance_m,
        ascent_m=_num(record[cols.ascent]),
        descent_m=_num(record[cols.descent]),
        avg_hr=_int(record[cols.avg_hr]),
        max_hr=_int(record[cols.max_hr]),
        avg_power=_num(record[cols.avg_power]),
        max_power=_num(record[cols.max_power]),
        np_watts=np_watts,
        # Garmin computed this NP over the full sample stream before exporting the
        # summary, so it is not the degraded estimate that np_reliable=false denotes.
        np_reliable=None if np_watts is None else True,
        avg_speed_mps=avg_speed_mps,
        avg_cadence=_num(record[cols.avg_run_cadence]),
        calories=_int(record[cols.calories]),
        ngp_speed_mps=ngp_speed_mps,
        gap_applied=gap_applied,
        swim_time_s=swim_time_s,
        swim_time_source=None if swim_time_s is None else "timer",
        # No sample was imported, so nothing here was assessed. 'unknown' is the honest
        # value: it means "not evaluated", where 'poor' would assert a measured verdict.
        data_quality="unknown",
        name=_clean(record[cols.title]),
        source="manual",
    )


def _metric_rows(
    activity: ActivityRow, thresholds: Sequence[Threshold], *, sex: Literal["male", "female"]
) -> tuple[ActivityMetricRow, ...]:
    discipline = DISCIPLINE_BY_SPORT_CODE[activity.sport_code]
    return build_activity_metric_rows(
        ActivityLoadInput(
            activity_id=activity.activity_id,
            sport_code=activity.sport_code,
            discipline=discipline,
            duration_s=float(activity.duration_s),
            distance_m=activity.distance_m,
            swim_time_s=activity.swim_time_s,
            np_watts=activity.np_watts,
            ngp_speed_mps=activity.ngp_speed_mps,
            # Empty by construction: a histogram needs samples, the export has none.
            hr_histogram={},
            threshold=threshold_for_date(thresholds, discipline, activity.local_date),
            all_threshold=threshold_for_date(thresholds, "all", activity.local_date),
            sex=sex,
        )
    )


def import_csv(
    conn: DBConnection,
    csv_path: Path,
    *,
    athlete_id: int,
    timezone: str,
    sex: Literal["male", "female"],
    dry_run: bool,
) -> tuple[int, int]:
    """Import every row of the export. Returns ``(activities, metric rows)``."""
    tz = ZoneInfo(timezone)
    # store.py and summary.py each declare their own structurally identical DBConnection
    # protocol; mypy treats the two as distinct nominal types, hence the cast.
    thresholds = load_thresholds(cast("SummaryDBConnection", conn), athlete_id=athlete_id)
    if not thresholds:
        print(
            f"WARNING: core.threshold holds no row for athlete_id={athlete_id}. "
            "Activities will be imported, but no TSS will be computed for any of them "
            "and mart.pmc will stay empty. Seed the thresholds first "
            "(docs/RUNBOOK.md section 1, step 3).",
            file=sys.stderr,
        )

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records:
        msg = f"{csv_path} is empty."
        raise CsvImportError(msg)
    cols = _resolve_columns(records[0])

    activities = 0
    metrics = 0
    skipped: list[str] = []
    for line_no, record in enumerate(records[1:], start=2):
        if not any(field.strip() for field in record):
            continue
        try:
            activity = build_activity_row(
                record, cols, athlete_id=athlete_id, tz=tz, line_no=line_no
            )
        except CsvImportError as exc:
            # The real export does contain unusable rows (e.g. an activity recorded with
            # a duration of 00:00:00, which CHECK activity_duration_pos rejects outright).
            # One corrupt row must not cost the other 299, but it is never dropped
            # silently: every skip is reported individually and counted in the summary.
            skipped.append(str(exc))
            continue
        rows = _metric_rows(activity, thresholds, sex=sex)
        activities += 1
        metrics += len(rows)
        if dry_run:
            continue
        store_activity_bundle(
            conn,
            CoreActivityBundle(
                activity=activity,
                samples=(),
                laps=(),
                swim_lengths=(),
                hr_histogram=(),
                power_curve=(),
                metrics=rows,
            ),
        )

    if skipped:
        print(f"{len(skipped)} row(s) skipped as unusable:", file=sys.stderr)
        for reason in skipped:
            print(f"  {reason}", file=sys.stderr)
    return activities, metrics


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/import_activities_csv.py",
        description=(
            "Load a Garmin Connect bulk export Activities.csv into core.activity and "
            "core.activity_metric, for schema validation against a real history."
        ),
    )
    parser.add_argument("csv_path", type=Path, help="Path to Activities.csv.")
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN. If omitted, read from T0_POSTGRES_DSN.",
    )
    parser.add_argument("--athlete-id", type=int, default=1, help="core.athlete.athlete_id.")
    parser.add_argument(
        "--timezone",
        default="Europe/Paris",
        help=(
            "IANA timezone the export local timestamps were recorded in. The CSV carries "
            "no UTC offset, so it cannot be inferred from the file."
        ),
    )
    parser.add_argument(
        "--sex", default="male", choices=["male", "female"], help="Used by the TRIMP formula."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and compute everything, write nothing. Reports what would be written.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)

    dsn = args.dsn or os.environ.get("T0_POSTGRES_DSN")
    if not dsn:
        print("No DSN: pass --dsn or set T0_POSTGRES_DSN.", file=sys.stderr)
        return 2

    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            # Same idiom as t0.transform.__main__: a real psycopg connection
            # satisfies the protocol at runtime, but its overloaded `cursor` signature
            # does not match the protocol's single one for mypy.
            activities, metrics = import_csv(
                cast("DBConnection", conn),
                args.csv_path,
                athlete_id=args.athlete_id,
                timezone=args.timezone,
                sex=args.sex,
                dry_run=args.dry_run,
            )
    except CsvImportError as exc:
        print(f"Import aborted: {exc}", file=sys.stderr)
        return 1

    verb = "would be written" if args.dry_run else "written"
    print(f"{activities} activity row(s) and {metrics} metric row(s) {verb}.")
    if not args.dry_run:
        print("Next: refresh the mart views with `python -m t0.ops refresh`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
