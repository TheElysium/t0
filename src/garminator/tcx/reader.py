"""Decode TCX file to shared intermediate structure.

Only module in ``garminator.tcx`` touching raw bytes/XML: mapping and model work
on extracted dicts. Same boundary as ``garminator.fit.reader``.

XML hardening (design choice, see batch end report): project has no ``defusedxml``
dependency (out of scope to add). This module hardens ``xml.etree.ElementTree``
manually via expat callbacks that reject all DOCTYPE and entity declarations—
protection against XXE and entity bomb attacks on external file (TCX is manual
athlete import, potentially untrusted content).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import xml.parsers.expat
from datetime import datetime
from typing import TYPE_CHECKING, Any

from garminator.fit.errors import TcxParseError
from garminator.fit.model import ParsedActivitySource
from garminator.tcx.mapping import TCD_NAMESPACE, TPX_NAMESPACE, map_trackpoint

if TYPE_CHECKING:
    from pathlib import Path

_NS = {"tcd": TCD_NAMESPACE, "tpx": TPX_NAMESPACE}


def _reject(*_args: object, **_kwargs: object) -> None:
    raise TcxParseError(
        "DOCTYPE or XML entity declaration refused (XXE protection): TCX file rejected."
    )


def _parse_xml_secure(data: bytes) -> ET.Element:
    """Parse XML rejecting all DOCTYPE/external entities (XXE hardening).

    ``defusedxml`` not a project dependency (out of scope to modify ``pyproject.toml``
    — see final report). Instead, minimal protection reproduced manually via
    ``xml.parsers.expat`` directly: ``ET.XMLParser().parser`` no longer exposes
    underlying expat parser in this environment (verified: ``dir(ET.XMLParser())``
    has no ``parser``), so we drive ``xml.parsers.expat.ParserCreate()`` manually
    with ``ET.TreeBuilder`` target, rather than wrapper ``ET.XMLParser``. Any
    DOCTYPE or entity declaration raises :class:`TcxParseError` before resolution,
    neutralizing both recursive entity expansion (bomb) and external resource read
    (XXE).
    """
    target = ET.TreeBuilder()
    # Separator "}" + _fixname: exactly reproduces ET.XMLParser behavior
    # (cf. xml.etree.ElementTree.XMLParser.__init__/_fixname) so tags passed to
    # TreeBuilder stay in Clark notation ("{namespace}tag"), required for
    # ``root.findall`` with ``_NS`` to keep working.
    expat_parser = xml.parsers.expat.ParserCreate(None, "}")
    names: dict[str, str] = {}

    def _fixname(key: str) -> str:
        try:
            return names[key]
        except KeyError:
            name = "{" + key if "}" in key else key
            names[key] = name
            return name

    def _start(tag: str, attr_list: list[str]) -> None:
        attrib = {_fixname(attr_list[i]): attr_list[i + 1] for i in range(0, len(attr_list), 2)}
        target.start(_fixname(tag), attrib)

    def _end(tag: str) -> None:
        target.end(_fixname(tag))

    # Expat callbacks each have different signatures (DOCTYPE, internal entity,
    # unparsed entity, external reference); `_reject` covers all via *args/**kwargs
    # (raises before even inspecting arguments). Only `ExternalEntityRefHandler` stub
    # has strict signature incompatible with mypy.
    expat_parser.StartDoctypeDeclHandler = _reject
    expat_parser.EntityDeclHandler = _reject
    expat_parser.UnparsedEntityDeclHandler = _reject
    expat_parser.ExternalEntityRefHandler = _reject  # type: ignore[assignment]
    expat_parser.ordered_attributes = True
    expat_parser.StartElementHandler = _start
    expat_parser.EndElementHandler = _end
    expat_parser.CharacterDataHandler = target.data

    try:
        expat_parser.Parse(data, True)
    except TcxParseError:
        raise
    except xml.parsers.expat.ExpatError as exc:
        raise TcxParseError(f"Malformed TCX XML: {exc}") from exc

    root = target.close()
    if root is None:
        raise TcxParseError("TCX XML empty or missing root element.")
    return root


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, _NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_trackpoint(tp: ET.Element) -> dict[str, Any]:
    """Extract XML Trackpoint to dict with normalized keys.

    Only fills what file actually contains: structure measured on ``bike.tcx``
    (addendum)—only Time/Cadence/Watts present, but reader stays generic for
    future TCX files carrying heart rate, distance, altitude, or position.
    """
    point: dict[str, Any] = {"time": _parse_time(_text(tp, "tcd:Time"))}

    hr = _text(tp, "tcd:HeartRateBpm/tcd:Value")
    if hr is not None:
        point["heart_rate_bpm"] = hr

    distance = _text(tp, "tcd:DistanceMeters")
    if distance is not None:
        point["distance_meters"] = distance

    altitude = _text(tp, "tcd:AltitudeMeters")
    if altitude is not None:
        point["altitude_meters"] = altitude

    lat = _text(tp, "tcd:Position/tcd:LatitudeDegrees")
    if lat is not None:
        point["latitude_degrees"] = lat
    lon = _text(tp, "tcd:Position/tcd:LongitudeDegrees")
    if lon is not None:
        point["longitude_degrees"] = lon

    # Cadence: either directly under Trackpoint (bike, standard TCD) or under
    # Extensions/TPX (Garmin variant)—first present source wins.
    cadence = _text(tp, "tcd:Cadence")
    if cadence is None:
        cadence = _text(tp, "tcd:Extensions/tpx:TPX/tpx:Cadence")
    if cadence is not None:
        point["cadence"] = cadence

    watts = _text(tp, "tcd:Extensions/tpx:TPX/tpx:Watts")
    if watts is not None:
        point["watts"] = watts

    return point


def decode_tcx(data: bytes, *, activity_id: int) -> ParsedActivitySource:
    """Decode TCX file (content already in memory) to :class:`ParsedActivitySource`.

    TCX has no ``session`` message (addendum §2.2): ``session`` always ``None``
    on output, and laps/lengths stay empty lists—nothing invented to fill native
    summary absence.

    Raises:
        TcxParseError: Malformed XML, DOCTYPE/entity rejected, or no usable
            trackpoint (with ``Time``) in file.
    """
    root = _parse_xml_secure(data)
    # `.//tcd:Activity` rather than a fixed `Activities/Activity` path: tolerates the
    # same structural variation `.//tcd:Trackpoint` below already tolerates, for one
    # attribute read rather than a whole new traversal.
    activity_element = root.find(".//tcd:Activity", _NS)
    tcx_sport = activity_element.get("Sport") if activity_element is not None else None
    trackpoints = root.findall(".//tcd:Trackpoint", _NS)

    points = [_extract_trackpoint(tp) for tp in trackpoints]
    points_with_time = [p for p in points if p.get("time") is not None]
    if trackpoints and not points_with_time:
        raise TcxParseError("No usable TCX Trackpoint with Time: file rejected.")

    start_time = points_with_time[0]["time"] if points_with_time else None

    samples = (
        [
            map_trackpoint(p, activity_id=activity_id, start_time=start_time)
            for p in points_with_time
        ]
        if start_time is not None
        else []
    )

    return ParsedActivitySource(
        source_format="tcx",
        samples=samples,
        laps=[],
        swim_lengths=[],
        session=None,
        start_time=start_time,
        tcx_sport=tcx_sport,
    )


def decode_tcx_file(path: Path, *, activity_id: int) -> ParsedActivitySource:
    """Variant of :func:`decode_tcx` that reads the file from disk."""
    return decode_tcx(path.read_bytes(), activity_id=activity_id)
