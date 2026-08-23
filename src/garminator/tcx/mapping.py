"""Mapping declaratif element TCX deja parse -> colonne core.sample.

Meme principe qu'en FIT (garminator.fit.mapping) : les fonctions ci-dessous ne
prennent jamais de texte XML brut, uniquement des dictionnaires deja extraits
par le lecteur (garminator.tcx.reader), pour rester testables sans fixture
binaire.

Structure mesuree sur bike.tcx (addendum) : par Trackpoint, seuls Time,
Cadence et Watts (sous Extensions/TPX, namespace ActivityExtension/v2) sont
presents. Frequence cardiaque, distance, vitesse, altitude, position sont
ABSENTS -- aucune valeur n'est inventee pour ces champs, ils restent None.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from garminator.fit.errors import TcxParseError
from garminator.fit.model import SampleRow

#: Namespace des extensions Garmin (cadence velo, watts) sur les Trackpoint TCX.
TPX_NAMESPACE = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

#: Namespace racine TrainingCenterDatabase.
TCD_NAMESPACE = "http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2"


def _to_float(raw: Any) -> float:
    """Convertit un texte XML en ``float``, en erreur TYPEE si le texte n'est pas un nombre.

    ``float(raw)`` nu laisserait remonter une :class:`ValueError` brute jusqu'a
    l'appelant, qui n'a alors qu'une trace Python la ou tout le reste du lot leve
    :class:`garminator.fit.errors.SourceParseError`.
    """
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise TcxParseError(f"Valeur numerique TCX illisible ({raw!r}) : ligne rejetee.") from exc


def _to_int(raw: Any) -> int:
    """Convertit un texte XML en ``int``, en tolerant la notation decimale.

    ``int("150")`` fonctionne mais ``int("150.0")`` leve : or rien n'oblige un
    exportateur TCX a ecrire les entiers sans partie decimale (le schema TCD type
    ``Watts`` en ``xsd:unsignedShort``, mais l'extension TPX n'est pas validee et les
    outils tiers ecrivent les deux). On passe donc par ``float`` puis on arrondit,
    plutot que de rejeter un fichier entier sur une virgule.
    """
    return round(_to_float(raw))


@dataclass(frozen=True, slots=True)
class TrackpointFieldSpec:
    """Une colonne core.sample cible et la cle du dict trackpoint qui la nourrit."""

    column: str
    source_key: str
    converter: Any = None


#: Table de correspondance trackpoint (dict deja extrait par le lecteur) -> core.sample.
#: Le dict source est produit par le lecteur avec des cles deja normalisees en minuscules
#: ASCII ("time", "heart_rate_bpm", "distance_meters", "altitude_meters", "cadence",
#: "watts", "latitude_degrees", "longitude_degrees") -- jamais les tags XML bruts.
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
    """Convertit un trackpoint deja extrait (dict) en :class:`SampleRow`.

    Raises:
        TcxParseError: si ``time`` est absent ou n'est pas un ``datetime`` -- un
            echantillon sans horodatage ne peut pas prendre place dans
            ``core.sample`` (cle primaire ``(activity_id, t)``).
    """
    t = point.get("time")
    if not isinstance(t, datetime):
        raise TcxParseError("Trackpoint TCX sans Time exploitable : ligne rejetee.")
    mapped: dict[str, Any] = {}
    for spec in TRACKPOINT_FIELD_SPECS:
        value = point.get(spec.source_key)
        if value is not None and spec.converter:
            value = spec.converter(value)
        mapped[spec.column] = value
    elapsed_s = round((t - start_time).total_seconds())
    return SampleRow(activity_id=activity_id, t=t, elapsed_s=elapsed_s, extra={}, **mapped)
