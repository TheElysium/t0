"""Pure tool handlers: query -> shape -> round, transport- and driver-free."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from t0.mcp.db import SqlRunner

DEFAULT_DIGITS = 2
HRV_RATIO_DIGITS = 3
# EF runs at ~0.02 m/s per bpm: 3 decimals would give one significant digit.
EF_DIGITS = 4


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


def _page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Uniform envelope for multi-row tools.

    A silently capped window would read as the full history: truncated=true
    means rows may extend beyond the window (narrow the range, raise limit)."""
    return {"rows": rows, "truncated": len(rows) == limit}


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
) -> dict[str, Any]:
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
    rows = [_shape_pmc(row) for row in reversed(run.fetch(_PMC_SQL, params))]
    return _page(rows, max_rows)


_ACTIVITY_COLUMNS = """
       w.activity_id, w.local_date, w.sport_code, w.sport, w.name,
       w.duration_s, w.swim_time_s, w.distance_m,
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
    distance = row["distance_m"]
    if distance == 0:
        # An ERG/home-trainer ride has no distance: zero would read as data.
        distance = None
    return {
        "activity_id": row["activity_id"],
        "date": _iso(row["local_date"]),
        "sport_code": row["sport_code"],
        "sport": row["sport"],
        "name": row["name"],
        "duration_s": row["duration_s"],
        # Active swim time (rest-at-wall excluded): the denominator of swim
        # sTSS/IF. duration_s is ELAPSED time and overstates swim load on
        # intervals -- never derive swim intensity from duration_s.
        "swim_time_s": row["swim_time_s"],
        "distance_m": distance,
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
        "tss_hr": round_number(row["tss_hr"]),
        "trimp": round_number(row["trimp"], 1),
        "intensity_factor": round_number(row["intensity_factor"]),
        "intensity_factor_method": row["intensity_factor_method"],
        "decoupling_pct": round_number(row["decoupling_pct"]),
        "efficiency_factor": round_number(row["efficiency_factor"], EF_DIGITS),
        "efficiency_factor_unit": _ef_unit(row["sport_code"]),
        # Always false: mart.activity_wide filters is_parent = false -- count
        # legs, never legs + parent.
        "is_parent": row["is_parent"],
    }


_EF_UNIT_BY_SPORT = {
    "running": "m/s per bpm",
    "trail_running": "m/s per bpm",
    "cycling": "W per bpm",
    "virtual_cycling": "W per bpm",
}


def _ef_unit(sport_code: str) -> str | None:
    """The two EF families are not comparable: speed-based vs power-based."""
    return _EF_UNIT_BY_SPORT.get(sport_code)


_SPORT_CODES_SQL = "SELECT DISTINCT sport_code FROM mart.activity_wide ORDER BY 1"


def _validated_sport_code(run: SqlRunner, sport_code: str | None) -> str | None:
    """Reject unknown sport codes LOUDLY: a silent [] would read as "no data"."""
    if sport_code is None:
        return None
    known = [r["sport_code"] for r in run.fetch(_SPORT_CODES_SQL, {})]
    if sport_code not in known:
        raise ValueError(f"unknown sport_code {sport_code!r}; valid values: {', '.join(known)}.")
    return sport_code


def list_activities(
    run: SqlRunner,
    *,
    max_rows: int,
    start_date: str | None = None,
    end_date: str | None = None,
    sport_code: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Recent sessions, newest first. ``limit`` bounds the window client-side;
    an out-of-vocabulary sport_code is an error, never a silent empty list."""
    _validated_sport_code(run, sport_code)
    effective_limit = min(limit, max_rows) if limit is not None else max_rows
    params = {
        "sport": sport_code,
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": effective_limit,
    }
    rows = [_shape_activity(row) for row in run.fetch(_LIST_ACTIVITIES_SQL, params)]
    return _page(rows, effective_limit)


def get_activity(run: SqlRunner, *, activity_id: int) -> dict[str, Any] | None:
    """One session by id, or None when unknown (a parent id yields None)."""
    row = run.fetch_one(_GET_ACTIVITY_SQL, {"activity_id": activity_id})
    return None if row is None else _shape_activity(row)


_POWER_CURVE_SQL = """
SELECT kind, duration_s, value, activity_id, local_date
  FROM mart.power_curve_best
 WHERE (%(kind)s::text IS NULL OR kind = %(kind)s)
   AND (%(start)s::date IS NULL OR local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR local_date <= %(end)s)
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
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """All-time record curve; ``date`` answers "what day did I set this record?".
    Dates narrow the contribution window, not the record values."""
    params = {
        "kind": kind,
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    rows = [_shape_power_curve(row) for row in run.fetch(_POWER_CURVE_SQL, params)]
    return _page(rows, max_rows)


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
) -> dict[str, Any]:
    """HRV / resting-HR readings with their 28-day rolling median baselines,
    chronological (latest max_rows nights when no range is given)."""
    params = {
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    rows = [_shape_wellness(row) for row in reversed(run.fetch(_WELLNESS_SQL, params))]
    return _page(rows, max_rows)


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
) -> dict[str, Any]:
    """Fired amber/red rules, one row per (day, rule), chronological
    (latest max_rows days when no range is given). Advisory only."""
    params = {
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    rows = [_shape_coach_flag(row) for row in reversed(run.fetch(_COACH_FLAGS_SQL, params))]
    return _page(rows, max_rows)


_RAMP_RISK_SQL = """
SELECT local_date, tss, trimp, ctl, atl, tsb, ctl_reliable,
       dctl_per_week, monotony, strain, estimated_share_7d
  FROM mart.ramp_risk
 WHERE (%(start)s::date IS NULL OR local_date >= %(start)s)
   AND (%(end)s::date IS NULL OR local_date <= %(end)s)
 ORDER BY local_date DESC
 LIMIT %(limit)s
"""


def _shape_ramp_risk(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": _iso(row["local_date"]),
        "tss": round_number(row["tss"]),
        "trimp": round_number(row["trimp"], 1),
        "ctl": round_number(row["ctl"]),
        "atl": round_number(row["atl"]),
        "tsb": round_number(row["tsb"]),
        "ctl_reliable": row["ctl_reliable"],
        "dctl_per_week": round_number(row["dctl_per_week"]),
        "monotony": round_number(row["monotony"], 2),
        "strain": round_number(row["strain"], 0),
        "estimated_share_7d": round_number(row["estimated_share_7d"], 3),
    }


def get_ramp_risk(
    run: SqlRunner,
    *,
    max_rows: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Per-day ramp risk: dCTL/week, Foster monotony/strain, 7-day estimated
    load share. The estimated share discounts CTL movement when high."""
    params = {
        "start": parse_iso_date(start_date, "start_date"),
        "end": parse_iso_date(end_date, "end_date"),
        "limit": max_rows,
    }
    rows = [_shape_ramp_risk(row) for row in reversed(run.fetch(_RAMP_RISK_SQL, params))]
    return _page(rows, max_rows)


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

_DISCIPLINES_SQL = "SELECT DISTINCT discipline FROM core.threshold ORDER BY 1"


def _validated_discipline(run: SqlRunner, discipline: str | None) -> str | None:
    """Same contract as the sport_code filter: invalid is an error, never []."""
    if discipline is None:
        return None
    known = [r["discipline"] for r in run.fetch(_DISCIPLINES_SQL, {})]
    if discipline not in known:
        raise ValueError(f"unknown discipline {discipline!r}; valid values: {', '.join(known)}.")
    return discipline


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
    _validated_discipline(run, discipline)
    params = {"discipline": discipline, "limit": max_rows}
    return [_shape_threshold(row) for row in run.fetch(_THRESHOLDS_SQL, params)]
