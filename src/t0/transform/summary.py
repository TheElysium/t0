"""Translate a Garmin Connect activity JSON summary (``get_activities_by_date``, stored
as-is in ``raw.api_response``) into :class:`~t0.transform.activity.ActivityContext`,
and load thresholds (``core.threshold``) that this context needs.

MISSING LINK identified by grep (no ``typeKey`` occurrence in ``src/`` before this layer):
``build_core_rows`` (``t0.transform.activity``) can do everything from an
``ActivityContext``, but nothing in the repo knew yet how to build it from what Garmin
Connect actually returns. This module closes that gap, and only this one: no load
computation, no ``core.*`` writes happen here.

NON-NEGOTIABLE RULE OF THIS MODULE: an unknown Garmin ``typeKey`` from the mapping table
is NEVER guessed -- neither folded to ``running`` (most frequent sport, would be silent
worst-case), nor to ``other`` (which would make the activity disappear from load computation
unnoticed). Garmin adds ``typeKey`` without warning (new watch sports, reorganized
categories); the only safe treatment of an uncovered case is to stop and name precisely
what is missing, so a human can add the row to :data:`SPORT_CODE_BY_TYPE_KEY` knowingly --
misclassifying a bike ride as a run would silently skew that session's TSS, thus all
downstream PMC depending on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from t0.garmin.raw_store import payload_sha256
from t0.load.thresholds import Threshold, threshold_for_date
from t0.transform.activity import ActivityContext

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

#: Format of the two timestamps ``startTimeLocal``/``startTimeGMT`` returned by
#: ``get_activities_by_date`` -- VERIFIED in the layer spec, not inferred.
_GARMIN_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class SportInconnuError(ValueError):
    """``activityType.typeKey`` missing from :data:`SPORT_CODE_BY_TYPE_KEY`.

    Names the received ``typeKey`` and original ``activityId`` so the operator can add
    the missing row to the mapping table -- see module docstring for why this case is
    never guessed."""

    def __init__(self, type_key: str, activity_id: int) -> None:
        self.type_key = type_key
        self.activity_id = activity_id
        super().__init__(
            f"unknown Garmin typeKey {type_key!r} for activity {activity_id}: no matching "
            "sport_code in SPORT_CODE_BY_TYPE_KEY (t0.transform.summary). Add the "
            "missing row rather than guessing -- wrong classification would silently skew TSS."
        )


#: Mapping ``activityType.typeKey`` (Garmin Connect) -> ``core.sport.sport_code``
#: (seed from migrations/003_core_ref.sql, line 38 -- the only 12 existing sport_codes).
#: Not exhaustive on Garmin side by design (cf. module docstring): each missing entry must
#: raise :class:`SportInconnuError`, never fall back to a default.
#:
#: Non-obvious decisions, documented here rather than in code (nothing to verify at
#: runtime, only to review):
#: - ``indoor_running`` -> ``treadmill``: Garmin distinguishes "Indoor Running" (generic,
#:   no identified treadmill) from "Treadmill Running", but ``core.sport`` has only one
#:   indoor code for running (``treadmill``, ``is_indoor=true``) -- closest without
#:   inventing a second.
#: - ``track_running`` -> ``running``: outdoor track, neither indoor nor trail; the only
#:   standard outdoor "run" discipline available is ``running``.
#: - ``indoor_cycling`` -> ``virtual_cycling``: home-trainer without simulation (Garmin
#:   distinguishes from ``virtual_ride``/Zwift), but ``core.sport`` has only one indoor
#:   bike code (``virtual_cycling``, ``is_indoor=true``) -- consistent with this
#:   athlete's profile (bike power confirmed ONLY on home-trainer).
#: - ``gravel_cycling`` / ``mountain_biking`` -> ``cycling``: no dedicated gravel/MTB
#:   code in seed; ``cycling`` (outdoor, not indoor) is the only compatible outdoor
#:   "bike" discipline, without guessing a code that does not exist in the database.
SPORT_CODE_BY_TYPE_KEY: dict[str, str] = {
    "running": "running",
    "trail_running": "trail_running",
    "treadmill_running": "treadmill",
    "indoor_running": "treadmill",
    "track_running": "running",
    "cycling": "cycling",
    "road_biking": "cycling",
    "indoor_cycling": "virtual_cycling",
    "virtual_ride": "virtual_cycling",
    "gravel_cycling": "cycling",
    "mountain_biking": "cycling",
    "lap_swimming": "lap_swimming",
    "open_water_swimming": "open_water",
    "hiking": "hiking",
    "resort_skiing_snowboarding_ws": "resort_skiing",
    "multi_sport": "multisport",
    "transition": "transition",
    "strength_training": "strength",
}

#: Discipline by sport_code, copied EXACTLY from the seed of migrations/003_core_ref.sql
#: (line 38) -- never re-derived here ("the discipline column is structural, not
#: decorative", cf. COMMENT ON TABLE core.sport in the migration).
DISCIPLINE_BY_SPORT_CODE: dict[str, str] = {
    "running": "run",
    "trail_running": "run",
    "treadmill": "run",
    "cycling": "bike",
    "virtual_cycling": "bike",
    "lap_swimming": "swim",
    "open_water": "swim",
    "hiking": "other",
    "resort_skiing": "other",
    "multisport": "other",
    "transition": "other",
    "strength": "other",
}


def _sport_code_for_type_key(type_key: str, activity_id: int) -> str:
    sport_code = SPORT_CODE_BY_TYPE_KEY.get(type_key)
    if sport_code is None:
        raise SportInconnuError(type_key, activity_id)
    return sport_code


def _optional_int(value: Any) -> int | None:
    """``None`` stays ``None`` -- never replaced by 0 (central schema rule, cf.
    ``t0.transform.rows`` docstring).

    ROUNDING not truncation, and this is not style: Garmin returns
    ``duration``/``elapsedDuration``/``movingDuration`` in FLOATING seconds. But
    ``duration_s`` is rounded below (``int(round(...)``). Truncating here would produce,
    on a 3599.6s session, an ``elapsed_s`` of 3599 vs. a ``duration_s`` of 3600 -- i.e.,
    elapsed time LESS than session time, physically impossible and caused only by two
    inconsistent conversions. One convention for all duration fields.
    """
    return None if value is None else int(round(float(value)))


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _parse_garmin_time(raw: Any, *, field_name: str, activity_id: int) -> datetime:
    if raw is None:
        msg = (
            f"activity {activity_id}: field '{field_name}' missing from Garmin summary -- "
            "cannot compute tz_offset_s without guessing a timezone (cf. module "
            "docstring)."
        )
        raise ValueError(msg)
    try:
        return datetime.strptime(str(raw), _GARMIN_TIME_FORMAT)
    except ValueError as exc:
        msg = (
            f"activity {activity_id}: field '{field_name}'={raw!r} does not match "
            f"expected Garmin format {_GARMIN_TIME_FORMAT!r}."
        )
        raise ValueError(msg) from exc


def build_activity_context(summary: Mapping[str, Any]) -> ActivityContext:
    """Build an ``ActivityContext`` from a JSON summary as returned by
    ``get_activities_by_date`` (and stored as-is in ``raw.api_response.payload``).

    Accesses NO samples: anything this summary does not carry stays ``None``, it is
    ``build_core_rows`` (``t0.transform.activity``) that later decides if a
    computed fallback exists from samples (cf. ``ActivityContext`` docstring on
    ``distance_m``/``ascent_m``/``descent_m``).
    """
    activity_id_raw = summary.get("activityId")
    if activity_id_raw is None:
        msg = "Garmin summary missing 'activityId': cannot build ActivityContext."
        raise ValueError(msg)
    activity_id = int(activity_id_raw)

    activity_type = summary.get("activityType") or {}
    type_key = activity_type.get("typeKey")
    if type_key is None:
        msg = f"activity {activity_id}: 'activityType.typeKey' missing from Garmin summary."
        raise ValueError(msg)
    sport_code = _sport_code_for_type_key(str(type_key), activity_id)
    discipline = DISCIPLINE_BY_SPORT_CODE[sport_code]

    start_time_local = _parse_garmin_time(
        summary.get("startTimeLocal"), field_name="startTimeLocal", activity_id=activity_id
    )
    start_time_gmt = _parse_garmin_time(
        summary.get("startTimeGMT"), field_name="startTimeGMT", activity_id=activity_id
    )
    # Round to second: the two Garmin timestamps have no sub-second, floating difference
    # would be only a datetime arithmetic artifact, not real timezone offset.
    tz_offset_s = round((start_time_local - start_time_gmt).total_seconds())

    duration_raw = summary.get("duration")
    if duration_raw is None:
        msg = f"activity {activity_id}: 'duration' missing from Garmin summary."
        raise ValueError(msg)
    duration_s = int(round(float(duration_raw)))
    if duration_s <= 0:
        msg = (
            f"activity {activity_id}: duration={duration_raw!r} yields duration_s="
            f"{duration_s} <= 0 ('duration' field from Garmin summary, converted to seconds)."
        )
        raise ValueError(msg)

    parent_raw = summary.get("parentId")
    if parent_raw is None:
        parent_raw = summary.get("parentActivityId")
    parent_activity_id = _optional_int(parent_raw)

    return ActivityContext(
        activity_id=activity_id,
        sport_code=sport_code,
        discipline=discipline,
        duration_s=duration_s,
        start_time_local=start_time_local,
        tz_offset_s=tz_offset_s,
        garmin_type_key=str(type_key),
        elapsed_s=_optional_int(summary.get("elapsedDuration")),
        moving_s=_optional_int(summary.get("movingDuration")),
        distance_m=_optional_float(summary.get("distance")),
        ascent_m=_optional_float(summary.get("elevationGain")),
        descent_m=_optional_float(summary.get("elevationLoss")),
        calories=_optional_int(summary.get("calories")),
        name=summary.get("activityName"),
        parent_activity_id=parent_activity_id,
        # multisport is the only sport_code with produces_records=false AND explicitly
        # grouping children in the seed (COMMENT ON COLUMN core.sport.produces_records,
        # migrations/003) -- it is the reliable signal of a parent activity, not a guess
        # about JSON shape.
        is_parent=sport_code == "multisport",
        # dict(summary): json.dumps can only natively serialize a dict, not any Mapping
        # -- payload_sha256 uses `default=str` which, on an unrecognized object, would
        # replace it entirely with its repr() instead of hashing the content key by key.
        # Explicit conversion avoids this silent trap.
        summary_sha256=payload_sha256(dict(summary)),
    )


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Minimal structural view of a psycopg 3 connection for test injection -- same pattern
    as :class:`t0.garmin.raw_store.DBConnection` (cf. its docstring for full
    justification: no generic query utilities at L1, and a real psycopg 3 connection
    satisfies this protocol without conversion)."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def load_thresholds(conn: DBConnection, *, athlete_id: int) -> tuple[Threshold, ...]:
    """Read all thresholds for ``athlete_id`` from ``core.threshold`` and return as many
    :class:`~t0.load.thresholds.Threshold` objects.

    ``lower(validity)``/``upper(validity)`` extract the bounds of the half-open
    ``daterange`` IN SQL rather than parsing its text representation on the Python side
    (e.g. ``'[2026-01-01,)'``): PostgreSQL already knows this type natively, hand-reparsing
    would be a fragile reimplementation of an already-solved problem. A NULL upper bound
    (what ``upper()`` returns for an unbounded interval) designates the current threshold,
    consistent with ``Threshold.valid_to: date | None``.

    Read-only: never does date resolution here -- it is :func:`select_thresholds` (or
    directly :func:`t0.load.thresholds.threshold_for_date`) that applies the rule
    "a date not covered returns None, never a guessed threshold"."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT threshold_id, discipline, lower(validity), upper(validity),
                   ftp_watts, rftp_watts, ftp_pace_s_per_km, css_m_s,
                   lthr_bpm, hr_max_bpm, hr_rest_bpm
              FROM core.threshold
             WHERE athlete_id = %s
             ORDER BY discipline, lower(validity);
            """,
            (athlete_id,),
        )
        rows = cur.fetchall()
    return tuple(
        Threshold(
            threshold_id=int(row[0]),
            discipline=row[1],
            valid_from=row[2],
            valid_to=row[3],
            ftp_watts=row[4],
            rftp_watts=row[5],
            ftp_pace_s_per_km=row[6],
            css_m_s=row[7],
            lthr_bpm=row[8],
            hr_max_bpm=row[9],
            hr_rest_bpm=row[10],
        )
        for row in rows
    )


def select_thresholds(
    thresholds: Sequence[Threshold], discipline: str, on_date: date
) -> tuple[Threshold | None, Threshold | None]:
    """Return ``(threshold, all_threshold)`` for ``discipline`` on ``on_date`` -- the two
    exact arguments separately expected by ``build_core_rows`` (``threshold`` carries
    FTP/CSS/threshold pace by discipline, ``all_threshold`` carries LTHR and max/rest HR,
    common to all disciplines).

    ``core.threshold.weight_kg`` exists in the database (migrations/003) but is NOT loaded
    here: no load computation reads it today. Carrying it in ``Threshold`` without readers
    would make it a dead field that looks current.

    Simple composition of :func:`t0.load.thresholds.threshold_for_date`, called
    twice: no resolution logic is duplicated here, only assembled to spare the runner's
    caller from doing it themselves per activity."""
    return (
        threshold_for_date(thresholds, discipline, on_date),
        threshold_for_date(thresholds, "all", on_date),
    )
