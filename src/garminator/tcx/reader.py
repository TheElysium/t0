"""Decodage d'un fichier TCX vers la structure intermediaire commune.

Seul module de ``garminator.tcx`` qui touche des octets/XML brut : mapping et
modele travaillent sur des dictionnaires deja extraits. Meme frontiere que
``garminator.fit.reader``.

Durcissement XML (decision discutable, voir rapport de fin de lot) : le projet
n'a pas ``defusedxml`` en dependance (hors perimetre de ce lot d'y toucher).
Ce module durcit donc ``xml.etree.ElementTree`` a la main via des callbacks
expat qui refusent toute DOCTYPE et toute declaration d'entite -- protection
contre les attaques XXE / bombe d'entites sur un fichier externe (le TCX est
un import manuel de l'athlete, donc un contenu potentiellement non fiable).
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
        "DOCTYPE ou declaration d'entite XML refusee (protection anti-XXE) : " "fichier TCX rejete."
    )


def _parse_xml_secure(data: bytes) -> ET.Element:
    """Parse le XML en refusant toute DOCTYPE/entite externe (durcissement anti-XXE).

    ``defusedxml`` n'est pas une dependance du projet (hors perimetre de ce lot
    de modifier ``pyproject.toml`` -- voir rapport final). A defaut, on
    reproduit sa protection minimale a la main via ``xml.parsers.expat``
    directement : ``ET.XMLParser().parser`` n'expose plus le parseur expat
    sous-jacent dans cet environnement (verifie : ``dir(ET.XMLParser())`` ne
    contient pas ``parser``), donc on pilote ``xml.parsers.expat.ParserCreate()``
    a la main, avec un ``ET.TreeBuilder`` comme cible, plutot que de passer par
    le wrapper ``ET.XMLParser``. Toute declaration de DOCTYPE ou d'entite fait
    lever :class:`TcxParseError` avant qu'elle ne soit resolue, ce qui
    neutralise a la fois l'expansion d'entites recursive (bombe) et la lecture
    de ressource externe (XXE).
    """
    target = ET.TreeBuilder()
    # Separateur "}" + _fixname : reproduit exactement le comportement de
    # ET.XMLParser (cf. xml.etree.ElementTree.XMLParser.__init__/_fixname)
    # pour que les tags rendus au TreeBuilder restent en notation Clark
    # ("{namespace}tag"), condition necessaire pour que ``root.findall``
    # avec ``_NS`` continue de fonctionner comme avant.
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

    # Les callbacks expat ont chacun une signature differente (DOCTYPE, entite interne,
    # entite non analysee, reference externe) ; `_reject` les couvre toutes via
    # *args/**kwargs (elle leve avant meme de regarder ses arguments). Seul le stub de
    # `ExternalEntityRefHandler` impose une signature stricte incompatible avec mypy.
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
        raise TcxParseError(f"XML TCX malforme : {exc}") from exc

    root = target.close()
    if root is None:
        raise TcxParseError("XML TCX vide ou sans element racine.")
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
    """Extrait un ``Trackpoint`` XML vers un dict a cles normalisees.

    Ne remplit que ce que le fichier contient reellement : structure mesuree
    sur ``bike.tcx`` (addendum), seuls Time/Cadence/Watts sont presents, mais
    ce lecteur reste generique pour les futurs TCX qui porteraient FC,
    distance, altitude ou position.
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

    # Cadence : soit directement sous Trackpoint (velo, TCD standard), soit
    # sous Extensions/TPX (variante Garmin) -- la premiere source presente
    # l'emporte.
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
    """Decode un fichier TCX (contenu deja lu en memoire) en :class:`ParsedActivitySource`.

    Le TCX n'a pas de message ``session`` (addendum SS2.2) : ``session`` vaut
    toujours ``None`` en sortie, et les tours/longueurs restent des listes
    vides -- rien n'est invente pour combler l'absence de resume natif.

    Raises:
        TcxParseError: XML malforme, DOCTYPE/entite refusee, ou aucun
            trackpoint exploitable (sans ``Time``) dans le fichier.
    """
    root = _parse_xml_secure(data)
    trackpoints = root.findall(".//tcd:Trackpoint", _NS)

    points = [_extract_trackpoint(tp) for tp in trackpoints]
    points_with_time = [p for p in points if p.get("time") is not None]
    if trackpoints and not points_with_time:
        raise TcxParseError("Aucun Trackpoint TCX avec Time exploitable : fichier rejete.")

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
    )


def decode_tcx_file(path: Path, *, activity_id: int) -> ParsedActivitySource:
    """Variante de :func:`decode_tcx` qui lit le fichier depuis le disque."""
    return decode_tcx(path.read_bytes(), activity_id=activity_id)
