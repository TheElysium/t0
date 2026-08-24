"""Declarative mapping parsed TCX element → core.sample column.

Same principle as FIT (garminator.fit.mapping): functions below never take raw
XML, only dicts already extracted by reader (garminator.tcx.reader), to stay
testable without binary fixture.

Structure measured on bike.tcx (addendum): per Trackpoint, only Time, Cadence,
and Watts (under Extensions/TPX, ActivityExtension/v2 namespace) present. Heart
rate, distance, speed, altitude, position ABSENT—no value invented for these
fields, they stay None.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from garminator.fit.errors import TcxParseError
from garminator.fit.model import SampleRow

#: Namespace for Garmin extensions (bike cadence, watts) on TCX Trackpoints.
TPX_NAMESPACE = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

#: Root namespace TrainingCenterDatabase. The slash before ``v2`` is part of the
#: official URI: without it every lookup silently yields zero elements instead of
#: failing, so a decode returns an empty activity rather than an error. Guarded by
#: test_tcd_namespace_matches_the_official_uri, which compares against a literal
#: rather than against a fixture -- fixtures are written to match this constant and
#: would keep agreeing with it while both are wrong.
TCD_NAMESPACE = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"


def _to_float(raw: Any) -> float:
    """Convert XML text to ``float``, raising typed error if text not a number.

    Bare ``float(raw)`` would let raw :class:`ValueError` bubble up to caller,
    who only sees Python trace where rest of batch raises
    :class:`garminator.fit.errors.SourceParseError`.
    """
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise TcxParseError(f"Unreadable TCX numeric value ({raw!r}): row rejected.") from exc


def _to_int(raw: Any) -> int:
    """Convert XML text to ``int``, tolerating decimal notation.

    ``int("150")`` works but ``int("150.0")`` raises: nothing forces TCX
    exporter to write integers without decimal part (TCD schema types ``Watts``
    as ``xsd:unsignedShort``, but TPX extension unvalidated and third-party
    tools write both). Go through ``float`` then round rather than reject entire
    file over a comma.
    """
    return round(_to_float(raw))


@dataclass(frozen=True, slots=True)
class TrackpointFieldSpec:
    """Target core.sample column and trackpoint dict key that feeds it."""

    column: str
    source_key: str
    converter: Any = None


#: Mapping table trackpoint (dict already extracted by reader) → core.sample.
#: Source dict produced by reader with keys already normalized to lowercase ASCII
#: ("time", "heart_rate_bpm", "distance_meters", "altitude_meters", "cadence",
#: "watts", "latitude_degrees", "longitude_degrees")—never raw XML tags.
TRACKPOINT_FIELD_SPECS: tuple[TrackpointFieldSpec, ...] = (
    TrackpointFieldSpec("heart_rate", "heart_rate_bpm", _to_int),
    TrackpointFieldSpec("distance_m", "distance_meters", _to_float),
    TrackpointFieldSpec("altitude_m", "altitude_meters", _to_float),
    TrackpointFieldSpec("cadence", "cadence", _to_int),
    TrackpointFieldSpec("power", "watts", _to_int),
    TrackpointFieldSpec("latitude_deg", "latitude_degrees", _to_float),
    TrackpointFieldSpec("longitude_deg", "longitude_degrees", _to_float),
)


def map_trackpoint(point: dict[str, Any], *, activity_id: int, start_time: datetime) -> SampleRow:
    """Convert already-extracted trackpoint (dict) to :class:`SampleRow`.

    Raises:
        TcxParseError: if ``time`` missing or not a ``datetime``—sample without
            timestamp cannot fit in ``core.sample`` (primary key ``(activity_id, t)``).
    """
    t = point.get("time")
    if not isinstance(t, datetime):
        raise TcxParseError("TCX Trackpoint missing usable Time: row rejected.")
    mapped: dict[str, Any] = {}
    for spec in TRACKPOINT_FIELD_SPECS:
        value = point.get(spec.source_key)
        if value is not None and spec.converter:
            value = spec.converter(value)
        mapped[spec.column] = value
    elapsed_s = round((t - start_time).total_seconds())
    return SampleRow(activity_id=activity_id, t=t, elapsed_s=elapsed_s, extra={}, **mapped)
