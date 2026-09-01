"""Pure tool handlers: query -> shape -> round, transport- and driver-free."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from t0.mcp.db import SqlRunner

DEFAULT_DIGITS = 2
HRV_RATIO_DIGITS = 3


def round_number(value: Any, digits: int = DEFAULT_DIGITS) -> float | None:
    """Round to display precision; None stays None. The pre-pass at digits + 2
    strips the float noise a numeric/real conversion reintroduces."""
    if value is None:
        return None
    return round(round(float(value), digits + 2), digits)


def _iso(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def parse_iso_date(raw: str | None, argument_name: str) -> date | None:
    """Validate an optional ISO date argument, error naming the argument only."""
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(
            f"{argument_name} must be an ISO date (YYYY-MM-DD), got an unparseable value."
        ) from None


_PMC_SQL = """
SELECT p.local_date, p.tss, p.ctl, p.atl, p.tsb, p.tsb_same_day,
       p.ctl_reliable, dl.has_estimated_load
  FROM mart.pmc p
  LEFT JOIN mart.daily_load dl
         ON dl.athlete_id = p.athlete_id AND dl.local_date = p.local_date
 WHERE (%(athlete_id)s::int IS NULL OR p.athlete_id = %(athlete_id)s)
   AND (%(start)s::date IS NULL OR p.local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR p.local_date <= %(end)s)
 ORDER BY p.local_date DESC
 LIMIT %(limit)s
"""


def _shape_pmc(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": _iso(row["local_date"]),
        "tss": round_number(row["tss"]),
        "ctl": round_number(row["ctl"]),
        "atl": round_number(row["atl"]),
        "tsb": round_number(row["tsb"]),
        "tsb_same_day": round_number(row["tsb_same_day"]),
        "ctl_reliable": row["ctl_reliable"],
        "has_estimated_load": row["has_estimated_load"],
    }


def get_pmc(
    run: SqlRunner,
    *,
    max_rows: int,
    athlete_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Performance Management Chart rows, dense calendar, chronological.

    The DESC order keeps the LIMIT window on the RECENT side; the Python
    reversal restores chronological presentation: a bare get_pmc() call
    (no dates) returns the latest max_rows days, never the oldest."""
    params = {
        "athlete_id": athlete_id,
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    return [_shape_pmc(row) for row in reversed(run.fetch(_PMC_SQL, params))]


_ACTIVITY_COLUMNS = """
       w.activity_id, w.local_date, w.sport_code, w.sport, w.name,
       w.duration_s, w.distance_m,
       w.avg_hr, w.max_hr, w.avg_power, w.np_watts, w.np_reliable,
       w.ascent_m, w.data_quality, w.needs_manual_rpe,
       w.tss, w.tss_method, w.tss_hr, w.trimp,
       w.intensity_factor, w.intensity_factor_method,
       w.decoupling_pct, w.efficiency_factor,
       a.is_parent
"""

_ACTIVITY_FILTERS = """
 WHERE (%(sport)s::text IS NULL OR w.sport_code = %(sport)s)
   AND (%(start)s::date IS NULL OR w.local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR w.local_date <= %(end)s)
"""

_LIST_ACTIVITIES_SQL = (
    "SELECT "  # noqa: S608 -- constant fragments, bound parameters only
    + _ACTIVITY_COLUMNS
    + "  FROM mart.activity_wide w"
    + "  JOIN core.activity a ON a.activity_id = w.activity_id"
    + _ACTIVITY_FILTERS
    + " ORDER BY w.local_date DESC, w.activity_id DESC"
    + " LIMIT %(limit)s"
)

_GET_ACTIVITY_SQL = (
    "SELECT "  # noqa: S608 -- constant fragments, bound parameters only
    + _ACTIVITY_COLUMNS
    + "  FROM mart.activity_wide w"
    + "  JOIN core.activity a ON a.activity_id = w.activity_id"
    + " WHERE w.activity_id = %(activity_id)s"
    + " LIMIT 1"
)


def _shape_activity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "activity_id": row["activity_id"],
        "date": _iso(row["local_date"]),
        "sport_code": row["sport_code"],
        "sport": row["sport"],
        "name": row["name"],
        "duration_s": row["duration_s"],
        "distance_m": row["distance_m"],
        "avg_hr": round_number(row["avg_hr"], 0),
        "max_hr": row["max_hr"],
        "avg_power": round_number(row["avg_power"], 0),
        "np_watts": round_number(row["np_watts"], 0),
        "np_reliable": row["np_reliable"],
        "ascent_m": row["ascent_m"],
        "data_quality": row["data_quality"],
        "needs_manual_rpe": row["needs_manual_rpe"],
        "tss": round_number(row["tss"]),
        "tss_method": row["tss_method"],
        # The view exposes a TSS only when the canonical arbitration produced one.
        "tss_is_canonical": row["tss"] is not None,
        "tss_hr": round_number(row["tss_hr"]),
        "trimp": round_number(row["trimp"], 1),
        "intensity_factor": round_number(row["intensity_factor"]),
        "intensity_factor_method": row["intensity_factor_method"],
        "decoupling_pct": round_number(row["decoupling_pct"]),
        "efficiency_factor": round_number(row["efficiency_factor"]),
        # Always false: mart.activity_wide filters is_parent = false -- count
        # legs, never legs + parent.
        "is_parent": row["is_parent"],
    }


def list_activities(
    run: SqlRunner,
    *,
    max_rows: int,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_code: str | None = None,
) -> list[dict[str, Any]]:
    """Recent sessions, newest first."""
    params = {
        "sport": sport_code,
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    return [_shape_activity(row) for row in run.fetch(_LIST_ACTIVITIES_SQL, params)]


def get_activity(run: SqlRunner, *, activity_id: int) -> dict[str, Any] | None:
    """One session by id, or None when unknown (a parent id yields None)."""
    row = run.fetch_one(_GET_ACTIVITY_SQL, {"activity_id": activity_id})
    return None if row is None else _shape_activity(row)


_POWER_CURVE_SQL = """
SELECT kind, duration_s, value, activity_id, local_date
  FROM mart.power_curve_best
 WHERE (%(kind)s::text IS NULL OR kind = %(kind)s)
 ORDER BY kind, duration_s
 LIMIT %(limit)s
"""


def _shape_power_curve(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "duration_s": row["duration_s"],
        "value": round_number(row["value"], 1),
        "activity_id": row["activity_id"],
        "date": _iso(row["local_date"]),
    }


def get_power_curve(
    run: SqlRunner,
    *,
    max_rows: int,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """All-time record curve; ``date`` answers "what day did I set this record?"."""
    params = {"kind": kind, "limit": max_rows}
    return [_shape_power_curve(row) for row in run.fetch(_POWER_CURVE_SQL, params)]


_WELLNESS_SQL = """
SELECT local_date, hrv_value, hrv_baseline, hrv_dev_ratio,
       rhr_value, rhr_baseline, rhr_dev_bpm
  FROM mart.wellness_baseline
 WHERE (%(start)s::date IS NULL OR local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR local_date <= %(end)s)
 ORDER BY local_date DESC
 LIMIT %(limit)s
"""


def _shape_wellness(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": _iso(row["local_date"]),
        "hrv_value": round_number(row["hrv_value"], 1),
        "hrv_baseline": round_number(row["hrv_baseline"], 1),
        "hrv_dev_ratio": round_number(row["hrv_dev_ratio"], HRV_RATIO_DIGITS),
        "rhr_value": round_number(row["rhr_value"], 1),
        "rhr_baseline": round_number(row["rhr_baseline"], 1),
        "rhr_dev_bpm": round_number(row["rhr_dev_bpm"], 1),
    }


def get_wellness(
    run: SqlRunner,
    *,
    max_rows: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """HRV / resting-HR readings with their 28-day rolling median baselines,
    chronological (latest max_rows nights when no range is given)."""
    params = {
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    return [_shape_wellness(row) for row in reversed(run.fetch(_WELLNESS_SQL, params))]


_COACH_FLAGS_SQL = """
SELECT local_date, rule_code, level, detail
  FROM mart.coach_flags
 WHERE (%(start)s::date IS NULL OR local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR local_date <= %(end)s)
 ORDER BY local_date DESC, rule_code
 LIMIT %(limit)s
"""


def _shape_coach_flag(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": _iso(row["local_date"]),
        "rule_code": row["rule_code"],
        "level": row["level"],
        "detail": row["detail"],
    }


def get_coach_flags(
    run: SqlRunner,
    *,
    max_rows: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fired amber/red rules, one row per (day, rule), chronological
    (latest max_rows days when no range is given). Advisory only."""
    params = {
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    return [_shape_coach_flag(row) for row in reversed(run.fetch(_COACH_FLAGS_SQL, params))]


_THRESHOLDS_SQL = """
SELECT threshold_id, discipline,
       lower(validity)::text AS valid_from, upper(validity)::text AS valid_to,
       ftp_watts, rftp_watts, ftp_pace_s_per_km, css_m_s, lthr_bpm,
       hr_max_bpm, hr_rest_bpm, weight_kg, power_source, source,
       css_estimated, note
  FROM core.threshold
 WHERE (%(discipline)s::text IS NULL OR discipline = %(discipline)s)
 ORDER BY discipline, lower(validity)
 LIMIT %(limit)s
"""


def _shape_threshold(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "threshold_id": row["threshold_id"],
        "discipline": row["discipline"],
        # Half-open [valid_from, valid_to); "infinity" = current threshold.
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "ftp_watts": round_number(row["ftp_watts"], 1),
        "rftp_watts": round_number(row["rftp_watts"], 1),
        "ftp_pace_s_per_km": round_number(row["ftp_pace_s_per_km"], 2),
        "css_m_s": round_number(row["css_m_s"], 3),
        "lthr_bpm": row["lthr_bpm"],
        "hr_max_bpm": row["hr_max_bpm"],
        "hr_rest_bpm": row["hr_rest_bpm"],
        "weight_kg": round_number(row["weight_kg"], 1),
        "power_source": row["power_source"],
        "source": row["source"],
        "css_estimated": row["css_estimated"],
        "note": row["note"],
    }


def get_thresholds(
    run: SqlRunner,
    *,
    max_rows: int,
    discipline: str | None = None,
) -> list[dict[str, Any]]:
    """Historized physiological thresholds, by validity period."""
    params = {"discipline": discipline, "limit": max_rows}
    return [_shape_threshold(row) for row in run.fetch(_THRESHOLDS_SQL, params)]
