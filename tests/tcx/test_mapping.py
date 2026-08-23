"""Tests du mapping declaratif TCX -> core.sample (sur dictionnaires deja extraits).

Comme pour le FIT, aucune fixture XML/binaire ici : uniquement des dictionnaires
dans la forme produite par garminator.tcx.reader._extract_trackpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from garminator.fit.errors import TcxParseError
from garminator.tcx.mapping import map_trackpoint

T0 = datetime(2026, 8, 1, 6, 0, 0, tzinfo=UTC)


def test_map_trackpoint_maps_cadence_and_watts_only() -> None:
    point = {"time": T0, "cadence": "80", "watts": "150"}

    row = map_trackpoint(point, activity_id=4, start_time=T0)

    assert row.activity_id == 4
    assert row.elapsed_s == 0
    assert row.cadence == 80
    assert row.power == 150
    # Structure mesuree sur bike.tcx (addendum) : FC, distance, vitesse, altitude,
    # position sont absents -- jamais inventes, restent None.
    assert row.heart_rate is None
    assert row.distance_m is None
    assert row.altitude_m is None
    assert row.latitude_deg is None


def test_map_trackpoint_without_time_raises() -> None:
    with pytest.raises(TcxParseError, match="Time"):
        map_trackpoint({"cadence": "80"}, activity_id=1, start_time=T0)


def test_map_trackpoint_elapsed_s_relative_to_start_time() -> None:
    t = datetime(2026, 8, 1, 6, 0, 12, tzinfo=UTC)
    row = map_trackpoint({"time": t}, activity_id=1, start_time=T0)
    assert row.elapsed_s == 12


def test_map_trackpoint_converts_optional_numeric_fields() -> None:
    point = {
        "time": T0,
        "heart_rate_bpm": "142",
        "distance_meters": "1234.5",
        "altitude_meters": "88.0",
        "latitude_degrees": "45.5",
        "longitude_degrees": "-1.25",
    }
    row = map_trackpoint(point, activity_id=1, start_time=T0)

    assert row.heart_rate == 142
    assert row.distance_m == pytest.approx(1234.5)
    assert row.altitude_m == pytest.approx(88.0)
    assert row.latitude_deg == pytest.approx(45.5)
    assert row.longitude_deg == pytest.approx(-1.25)


# --- Regression : conversions numeriques robustes (_to_int / _to_float) -------------
# Avant correction, TRACKPOINT_FIELD_SPECS utilisait `int` / `float` nus : un
# <Watts>N/A</Watts> remontait une ValueError brute (pas une SourceParseError) et
# un <Watts>150.0</Watts> faisait echouer tout le fichier.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("150", 150), ("150.0", 150), ("150.6", 151), ("  42 ", 42)],
)
def test_map_trackpoint_accepts_decimal_notation_for_integer_columns(
    raw: str, expected: int
) -> None:
    row = map_trackpoint({"time": T0, "watts": raw}, activity_id=1, start_time=T0)
    assert row.power == expected


@pytest.mark.parametrize("raw", ["N/A", "", "   ", "abc", "1,5"])
def test_map_trackpoint_rejects_non_numeric_with_typed_error(raw: str) -> None:
    """Une valeur presente mais illisible est une erreur TYPEE, pas une ValueError brute.

    Cas ``""`` inclus : le lecteur ne pose la cle que si l'element existe, donc une
    chaine vide vient d'un ``<Watts></Watts>`` reellement present dans le fichier --
    c'est un fichier malforme, pas un champ absent (un champ absent n'a pas de cle).
    """
    with pytest.raises(TcxParseError, match="illisible"):
        map_trackpoint({"time": T0, "watts": raw}, activity_id=1, start_time=T0)


def test_map_trackpoint_never_substitutes_zero_for_absent_fields() -> None:
    """Point 2 : un champ absent reste None, jamais 0 (0 W est une valeur mesuree)."""
    row = map_trackpoint({"time": T0}, activity_id=1, start_time=T0)
    for column in (
        "heart_rate",
        "distance_m",
        "altitude_m",
        "cadence",
        "power",
        "latitude_deg",
        "longitude_deg",
        "speed_mps",
    ):
        assert getattr(row, column) is None, column
    # ... alors qu'un zero explicitement present est bien conserve.
    zero = map_trackpoint({"time": T0, "watts": "0"}, activity_id=1, start_time=T0)
    assert zero.power == 0
