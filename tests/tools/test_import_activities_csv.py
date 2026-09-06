"""Unit tests for the one-off bulk-export importer (tools/import_activities_csv.py).

Every record below is SYNTHETIC. The real export carries GPS traces and is never used
as a fixture. The module lives outside the ``t0`` package, so it is loaded by
path rather than imported: ``tools`` is deliberately not an installable package."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "import_activities_csv.py"
_SPEC = importlib.util.spec_from_file_location("_import_activities_csv", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
importer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = importer
_SPEC.loader.exec_module(importer)

PARIS = ZoneInfo("Europe/Paris")

#: Header limited to the columns actually read, in the order _Columns resolves them.
#: The trailing duplicate reproduces the export's real quirk: the cadence header appears
#: twice (running, then cycling) and only the first occurrence must be picked up.
HEADER = [
    "Type d'activité",
    "Date",
    "Titre",
    "Distance",
    "Calories",
    "Durée",
    "Fréquence cardiaque moyenne",
    "Fréquence cardiaque maximale",
    "Allure moyenne",
    "Ascension totale",
    "Descente totale",
    "GAP moyenne",
    "Normalized Power® (NP®)",
    "Puissance moyenne",
    "Puissance max.",
    "Temps de déplacement",
    "Temps écoulé",
    "Cadence de course moyenne",
    "Cadence de course moyenne",
]

_RUN = {
    "Type d'activité": "Course à pied",
    "Date": "2025-07-14 08:30:00",
    "Titre": "Synthetic run",
    "Distance": "10.50",
    "Calories": "700",
    "Durée": "00:42:00",
    "Fréquence cardiaque moyenne": "155",
    "Fréquence cardiaque maximale": "175",
    "Allure moyenne": "4:00",
    "Ascension totale": "120",
    "Descente totale": "118",
    "GAP moyenne": "3:50",
    "Normalized Power® (NP®)": "290",
    "Puissance moyenne": "280",
    "Puissance max.": "400",
    "Temps de déplacement": "00:41:30",
    "Temps écoulé": "00:43:00",
    "Cadence de course moyenne": "172",
}


def record(**overrides: str) -> list[str]:
    """One synthetic export row, aligned on HEADER."""
    values = {**_RUN, **overrides}
    row = [values.get(name, "") for name in HEADER]
    row[-1] = "88"  # cycling cadence: must never be the one that is read
    return row


@pytest.fixture
def cols() -> Any:
    return importer._resolve_columns(HEADER)


def build(cols: Any, **overrides: str) -> Any:
    return importer.build_activity_row(record(**overrides), cols, athlete_id=1, tz=PARIS, line_no=2)


def test_resolve_columns_picks_the_first_duplicate_header(cols: Any) -> None:
    assert cols.avg_run_cadence == HEADER.index("Cadence de course moyenne")
    assert cols.avg_run_cadence != len(HEADER) - 1


def test_resolve_columns_reports_the_observed_header() -> None:
    with pytest.raises(importer.CsvImportError, match="is absent from the export header"):
        importer._resolve_columns([name for name in HEADER if name != "Durée"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:42:00", 2520.0),
        ("1:05:30", 3930.0),
        ("00:00:53.7", 53.7),
        ("--", None),
        ("", None),
    ],
)
def test_hms_parses_the_export_duration_forms(text: str, expected: float | None) -> None:
    assert importer._hms(text) == expected


def test_pace_to_mps_returns_none_for_the_kmh_form() -> None:
    """The pace column holds km/h for cycling: it has no colon and must not be parsed."""
    assert importer._pace_to_mps("28.4") is None
    assert importer._pace_to_mps("4:00") == pytest.approx(1000.0 / 240.0)


def test_num_strips_the_thousands_separator() -> None:
    assert importer._num("1,500") == 1500.0
    assert importer._num("'1,234.5") == 1234.5


def test_running_row_prefers_the_grade_adjusted_pace(cols: Any) -> None:
    activity = build(cols)
    assert activity.gap_applied is True
    assert activity.ngp_speed_mps == pytest.approx(1000.0 / 230.0)


def test_running_row_falls_back_to_the_raw_pace(cols: Any) -> None:
    activity = build(cols, **{"GAP moyenne": "--"})
    assert activity.gap_applied is False
    assert activity.ngp_speed_mps == pytest.approx(1000.0 / 240.0)


def test_swim_distance_is_already_in_metres(cols: Any) -> None:
    activity = build(
        cols,
        **{"Type d'activité": "Nat. piscine", "Distance": "1,500", "GAP moyenne": "--"},
    )
    assert activity.sport_code == "lap_swimming"
    assert activity.distance_m == 1500.0
    # A swim summary carries no per-length stream, so the timer is the only source.
    assert activity.swim_time_s == 2490
    assert activity.swim_time_source == "timer"
    # The pace column holds min/100 m for swimming: it must not leak into run_gap.
    assert activity.ngp_speed_mps is None


def test_cycling_distance_is_converted_from_kilometres(cols: Any) -> None:
    activity = build(cols, **{"Type d'activité": "Cyclisme", "Distance": "40.00"})
    assert activity.sport_code == "cycling"
    assert activity.distance_m == 40000.0
    assert activity.ngp_speed_mps is None
    assert activity.swim_time_s is None


def test_summary_normalized_power_is_flagged_reliable(cols: Any) -> None:
    assert build(cols).np_reliable is True
    assert build(cols, **{"Normalized Power® (NP®)": "--"}).np_reliable is None


def test_no_sample_means_the_quality_was_not_evaluated(cols: Any) -> None:
    assert build(cols).data_quality == "unknown"
    assert build(cols).source == "manual"


@pytest.mark.parametrize(
    ("local_date", "offset_s"),
    [("2025-07-14 08:30:00", 7200), ("2025-01-14 08:30:00", 3600)],
)
def test_daylight_saving_offsets_are_resolved(cols: Any, local_date: str, offset_s: int) -> None:
    activity = build(cols, Date=local_date)
    assert activity.tz_offset_s == offset_s
    assert activity.start_time_local == datetime.strptime(local_date, "%Y-%m-%d %H:%M:%S")
    assert activity.start_time == activity.start_time_local.replace(tzinfo=PARIS).astimezone(UTC)


def test_identifier_is_deterministic_and_inside_the_manual_range(cols: Any) -> None:
    """Re-importing the same export must not duplicate rows, and the identifiers must
    satisfy CHECK activity_manual_id_range without colliding with the upload sequence."""
    first, second = build(cols), build(cols)
    assert first.activity_id == second.activity_id
    # Since migration 024 the manual range is [1e15, 2e15): above real Garmin ids and
    # below the leg range, but crucially under the float64 exact-integer limit (2^53 ~
    # 9.007e15) so the id survives a JSON round-trip unrounded.
    assert 1_000_000_000_000_000 <= first.activity_id < 2_000_000_000_000_000
    assert first.activity_id < 2**53
    assert first.activity_id != build(cols, Date="2025-07-14 08:31:00").activity_id


def test_unusable_duration_is_rejected(cols: Any) -> None:
    """The real export does contain such a row; it must never be silently coerced."""
    with pytest.raises(importer.CsvImportError, match="unusable duration"):
        build(cols, **{"Durée": "00:00:00"})


def test_unknown_activity_type_is_rejected_rather_than_guessed(cols: Any) -> None:
    with pytest.raises(importer.CsvImportError, match="SPORT_CODE_BY_CSV_TYPE"):
        build(cols, **{"Type d'activité": "Padel"})
