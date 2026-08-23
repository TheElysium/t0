"""Tests du mapping declaratif FIT -> colonnes core.* (sur dictionnaires deja decodes).

Aucune fixture binaire ici : conformement a la contrainte du lot L3, ces tests
travaillent sur des dictionnaires Python construits a la main, dans la forme
que produit garmin_fit_sdk.Decoder.read() (verifiee sur running.fit/swim.fit
lors de l'investigation), jamais sur des octets .FIT.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from garminator.fit.errors import FitParseError
from garminator.fit.mapping import (
    derive_lap_index_for_lengths,
    map_lap,
    map_length,
    map_record,
    map_session,
    round_to_int,
    semicircles_to_degrees,
)

T0 = datetime(2026, 8, 1, 6, 0, 0, tzinfo=UTC)


def test_semicircles_to_degrees_none() -> None:
    assert semicircles_to_degrees(None) is None


def test_semicircles_to_degrees_known_value() -> None:
    # 2**31 semicircles == 180 degres (facteur de conversion FIT standard).
    assert semicircles_to_degrees(2**31) == pytest.approx(180.0)
    assert semicircles_to_degrees(2**30) == pytest.approx(90.0)


def test_round_to_int_none() -> None:
    assert round_to_int(None) is None


def test_round_to_int_rounds() -> None:
    assert round_to_int(12.6) == 13


def test_map_record_maps_known_fields_and_prefers_enhanced() -> None:
    msg = {
        "timestamp": datetime(2026, 8, 1, 6, 0, 10, tzinfo=UTC),
        "position_lat": 2**30,
        "position_long": 0,
        "altitude": 100.0,
        "enhanced_altitude": 101.5,
        "distance": 250.0,
        "heart_rate": 140,
        "cadence": 85,
        "power": 210,
        "unknown_field_123": 42,
    }
    row = map_record(msg, activity_id=7, start_time=T0)

    assert row.activity_id == 7
    assert row.elapsed_s == 10
    assert row.latitude_deg == pytest.approx(90.0)
    assert row.longitude_deg == pytest.approx(0.0)
    # enhanced_altitude doit l'emporter sur altitude quand les deux sont presents.
    assert row.altitude_m == 101.5
    assert row.distance_m == 250.0
    assert row.heart_rate == 140
    assert row.cadence == 85
    assert row.power == 210
    # Le champ non couvert par la table de mapping part dans extra, rien n'est perdu.
    assert row.extra == {"unknown_field_123": 42}


def test_map_record_without_timestamp_raises() -> None:
    with pytest.raises(FitParseError, match="timestamp"):
        map_record({"heart_rate": 100}, activity_id=1, start_time=T0)


def test_map_lap_maps_known_fields() -> None:
    msg = {
        "message_index": 2,
        "start_time": T0,
        "total_timer_time": 305.4,
        "total_distance": 1000.0,
        "avg_heart_rate": 150,
        "max_heart_rate": 172,
        "avg_power": 220.0,
        "num_lengths": 4,
    }
    row = map_lap(msg, activity_id=9)

    assert row.activity_id == 9
    assert row.lap_index == 2
    assert row.duration_s == 305  # arrondi, contrairement a swim_length
    assert row.distance_m == 1000.0
    assert row.avg_hr == 150
    assert row.max_hr == 172
    assert row.num_lengths == 4


def test_map_lap_without_start_time_raises() -> None:
    with pytest.raises(FitParseError, match="start_time"):
        map_lap({"message_index": 0, "total_timer_time": 10.0}, activity_id=1)


def test_map_lap_without_duration_raises() -> None:
    with pytest.raises(FitParseError, match="total_timer_time"):
        map_lap({"message_index": 0, "start_time": T0}, activity_id=1)


def test_map_length_active_preserves_float_duration() -> None:
    msg = {
        "message_index": 5,
        "start_time": T0,
        "total_timer_time": 21.34,
        "length_type": "active",
        "swim_stroke": "freestyle",
        "total_strokes": 18,
    }
    row = map_length(msg, activity_id=3, lap_index=1)

    assert row.length_index == 5
    assert row.lap_index == 1
    assert row.duration_s == pytest.approx(21.34)  # non arrondi (colonne real)
    assert row.length_type == "active"
    assert row.swim_stroke == "freestyle"
    assert row.total_strokes == 18


def test_map_length_idle_is_accepted() -> None:
    msg = {
        "message_index": 6,
        "start_time": T0,
        "total_timer_time": 15.0,
        "length_type": "idle",
    }
    row = map_length(msg, activity_id=3, lap_index=None)
    assert row.length_type == "idle"
    assert row.lap_index is None


def test_map_length_unexpected_length_type_raises() -> None:
    msg = {
        "message_index": 0,
        "start_time": T0,
        "total_timer_time": 1.0,
        "length_type": "bogus",
    }
    with pytest.raises(FitParseError, match="length_type"):
        map_length(msg, activity_id=1, lap_index=None)


def test_derive_lap_index_for_lengths_handles_contiguous_ranges_and_gaps() -> None:
    # Un tour de repos (num_lengths=0) ne doit rattacher aucune longueur, et ne
    # doit pas decaler la plage du tour suivant (cas reellement observe sur swim.fit).
    lap_msgs = [
        {"message_index": 0, "first_length_index": 0, "num_lengths": 2},
        {"message_index": 1, "first_length_index": 2, "num_lengths": 0},  # repos
        {"message_index": 2, "first_length_index": 2, "num_lengths": 3},
    ]
    result = derive_lap_index_for_lengths(lap_msgs)

    assert result == {0: 0, 1: 0, 2: 2, 3: 2, 4: 2}


def test_derive_lap_index_for_lengths_ignores_incomplete_laps() -> None:
    lap_msgs = [{"message_index": 0}]  # pas de first_length_index/num_lengths
    assert derive_lap_index_for_lengths(lap_msgs) == {}


def test_map_session_never_raises_on_missing_fields() -> None:
    summary = map_session({"total_elapsed_time": 3600.0})
    assert summary.training_stress_score is None
    assert summary.training_load_peak is None


def test_map_session_extracts_comparison_fields() -> None:
    msg = {
        "training_load_peak": 88.0,
        "total_training_effect": 3.2,
        "total_anaerobic_training_effect": 1.1,
        "workout_rpe": 6.0,
        "workout_feel": 80.0,
    }
    summary = map_session(msg)
    assert summary.training_load_peak == 88.0
    assert summary.total_training_effect == 3.2
    assert summary.training_stress_score is None  # absent des FIT reels, addendum SS1
    assert summary.raw == msg
