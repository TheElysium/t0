"""Pure handler tests: query -> shape -> round, no database."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from t0.mcp import handlers

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.mcp.conftest import FakeRunner

MAX_ROWS = 7


def _pmc_rows() -> list[dict[str, Any]]:
    # The SQL now returns DESC: newest day first.
    return [
        {
            "local_date": date(2026, 8, 31),
            "tss": None,
            "ctl": None,
            "atl": None,
            "tsb": None,
            "tsb_same_day": None,
            "ctl_reliable": True,
            "has_estimated_load": None,
        },
        {
            "local_date": date(2026, 8, 30),
            "tss": 61.300000000000004,
            "ctl": 42.005,
            "atl": 55.0,
            "tsb": None,
            "tsb_same_day": -13.005,
            "ctl_reliable": False,
            "has_estimated_load": True,
        },
    ]


def test_get_pmc_rounds_and_never_elides_flags(fake_runner: FakeRunner) -> None:
    fake_runner.queue("FROM MART.PMC", _pmc_rows())

    rows = handlers.get_pmc(
        fake_runner, max_rows=MAX_ROWS, start_date="2026-08-01", end_date="2026-08-31"
    )

    # The DESC LIMIT window is on the recent side; presentation stays chronological.
    assert [row["date"] for row in rows] == ["2026-08-30", "2026-08-31"]
    assert rows[1]["tss"] is None and rows[1]["ctl"] is None
    assert rows[0]["tss"] == 61.3
    assert rows[0]["ctl"] == 42.01
    assert rows[0]["tsb_same_day"] == -13.01
    assert rows[0]["ctl_reliable"] is False
    assert rows[0]["has_estimated_load"] is True
    assert rows[1]["ctl_reliable"] is True
    assert rows[1]["has_estimated_load"] is None


def test_every_handler_binds_the_safety_limit(fake_runner: FakeRunner) -> None:
    cases: list[tuple[str, Callable[[], Any]]] = [
        ("FROM MART.PMC", lambda: handlers.get_pmc(fake_runner, max_rows=MAX_ROWS)),
        (
            "FROM MART.ACTIVITY_WIDE",
            lambda: handlers.list_activities(fake_runner, max_rows=MAX_ROWS),
        ),
        (
            "FROM MART.POWER_CURVE_BEST",
            lambda: handlers.get_power_curve(fake_runner, max_rows=MAX_ROWS),
        ),
        (
            "FROM MART.WELLNESS_BASELINE",
            lambda: handlers.get_wellness(fake_runner, max_rows=MAX_ROWS),
        ),
        (
            "FROM MART.COACH_FLAGS",
            lambda: handlers.get_coach_flags(fake_runner, max_rows=MAX_ROWS),
        ),
        (
            "FROM CORE.THRESHOLD",
            lambda: handlers.get_thresholds(fake_runner, max_rows=MAX_ROWS),
        ),
    ]
    for marker, call in cases:
        fake_runner.queue(marker, [])
        call()
        sql, params = fake_runner.calls[-1]
        assert "LIMIT %(limit)s" in sql, f"{marker}: no bound LIMIT in {sql}"
        assert params["limit"] == MAX_ROWS, f"{marker}: LIMIT not fed with max_rows"


def test_list_activities_surfaces_method_flags_and_parent(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.queue(
        "FROM MART.ACTIVITY_WIDE",
        [
            {
                "activity_id": 12,
                "local_date": date(2026, 8, 30),
                "sport_code": "cycling",
                "sport": "Cycling",
                "name": "Sortie seuil",
                "duration_s": 5400,
                "distance_m": 42_195,
                "avg_hr": 148.6,
                "max_hr": 171,
                "avg_power": 210.0,
                "np_watts": 233.9887,
                "np_reliable": True,
                "ascent_m": 320,
                "data_quality": "good",
                "needs_manual_rpe": False,
                "tss": 58.7,
                "tss_method": "bike_power",
                "tss_hr": 63.2,
                "trimp": None,
                "intensity_factor": 0.9400000001,
                "intensity_factor_method": "bike_power",
                "decoupling_pct": 3.45,
                "efficiency_factor": 1.1234,
                "is_parent": False,
            }
        ],
    )

    rows = handlers.list_activities(fake_runner, max_rows=MAX_ROWS)

    row = rows[0]
    assert row["tss_method"] == "bike_power"
    assert row["tss_is_canonical"] is True
    assert row["intensity_factor_method"] == "bike_power"
    assert row["intensity_factor"] == 0.94
    assert row["np_reliable"] is True
    assert row["data_quality"] == "good"
    assert row["is_parent"] is False
    assert row["duration_s"] == 5400
    assert row["np_watts"] == 234.0
    assert row["avg_hr"] == 149
    assert row["trimp"] is None


def test_list_activities_null_tss_is_not_canonical(fake_runner: FakeRunner) -> None:
    fake_runner.queue(
        "FROM MART.ACTIVITY_WIDE",
        [
            {
                "activity_id": 13,
                "local_date": date(2026, 8, 29),
                "sport_code": "hiking",
                "sport": "Hiking",
                "name": None,
                "duration_s": 3600,
                "distance_m": None,
                "avg_hr": None,
                "max_hr": None,
                "avg_power": None,
                "np_watts": None,
                "np_reliable": None,
                "ascent_m": None,
                "data_quality": "unknown",
                "needs_manual_rpe": True,
                "tss": None,
                "tss_method": None,
                "tss_hr": None,
                "trimp": None,
                "intensity_factor": None,
                "intensity_factor_method": None,
                "decoupling_pct": None,
                "efficiency_factor": None,
                "is_parent": False,
            }
        ],
    )

    (row,) = handlers.list_activities(fake_runner, max_rows=MAX_ROWS)

    assert row["tss"] is None
    assert row["tss_method"] is None
    assert row["tss_is_canonical"] is False


def test_get_activity_unknown_id_returns_none(fake_runner: FakeRunner) -> None:
    fake_runner.queue("FROM MART.ACTIVITY_WIDE", [])

    assert handlers.get_activity(fake_runner, activity_id=999) is None


def test_get_power_curve_shapes_value_and_record_date(fake_runner: FakeRunner) -> None:
    fake_runner.queue(
        "FROM MART.POWER_CURVE_BEST",
        [
            {
                "kind": "power_pct_ftp",
                "duration_s": 1200,
                "value": 289.45,
                "activity_id": 7,
                "local_date": date(2026, 7, 14),
            }
        ],
    )

    rows = handlers.get_power_curve(fake_runner, max_rows=MAX_ROWS, kind="power_pct_ftp")

    assert rows[0] == {
        "kind": "power_pct_ftp",
        "duration_s": 1200,
        "value": 289.4,
        "activity_id": 7,
        "date": "2026-07-14",
    }


def test_get_wellness_rounds_the_deviation_ratio_at_three_decimals(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.queue(
        "FROM MART.WELLNESS_BASELINE",
        [
            {
                "local_date": date(2026, 8, 30),
                "hrv_value": 61.4,
                "hrv_baseline": 64.25,
                "hrv_dev_ratio": -0.1519999,
                "rhr_value": 49.0,
                "rhr_baseline": 47.3,
                "rhr_dev_bpm": 1.7,
            }
        ],
    )

    (row,) = handlers.get_wellness(fake_runner, max_rows=MAX_ROWS)

    assert row["hrv_dev_ratio"] == -0.152
    assert row["hrv_value"] == 61.4
    assert row["rhr_dev_bpm"] == 1.7


def test_get_wellness_preserves_chronological_order(fake_runner: FakeRunner) -> None:
    fake_runner.queue(
        "FROM MART.WELLNESS_BASELINE",
        [
            {
                "local_date": date(2026, 8, 31),
                "hrv_value": 60.0,
                "hrv_baseline": 64.0,
                "hrv_dev_ratio": None,
                "rhr_value": 49.0,
                "rhr_baseline": 47.0,
                "rhr_dev_bpm": None,
            },
            {
                "local_date": date(2026, 8, 30),
                "hrv_value": 61.4,
                "hrv_baseline": 64.25,
                "hrv_dev_ratio": -0.1519999,
                "rhr_value": 49.0,
                "rhr_baseline": 47.3,
                "rhr_dev_bpm": 1.7,
            },
        ],
    )

    rows = handlers.get_wellness(fake_runner, max_rows=MAX_ROWS)

    assert [row["date"] for row in rows] == ["2026-08-30", "2026-08-31"]


def test_get_coach_flags_passes_the_detail_through(fake_runner: FakeRunner) -> None:
    fake_runner.queue(
        "FROM MART.COACH_FLAGS",
        [
            {
                "local_date": date(2026, 8, 30),
                "rule_code": "tsb_below_minus30",
                "level": "amber",
                "detail": "TSB -34 < -30",
            }
        ],
    )

    rows = handlers.get_coach_flags(fake_runner, max_rows=MAX_ROWS)

    assert rows[0]["detail"] == "TSB -34 < -30"
    assert rows[0]["level"] == "amber"


def test_get_thresholds_keeps_validity_as_text_and_css_estimated(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.queue(
        "FROM CORE.THRESHOLD",
        [
            {
                "threshold_id": 3,
                "discipline": "bike",
                "valid_from": "2026-03-01",
                "valid_to": "infinity",
                "ftp_watts": 245.0,
                "rftp_watts": None,
                "ftp_pace_s_per_km": None,
                "css_m_s": None,
                "lthr_bpm": 152,
                "hr_max_bpm": 188,
                "hr_rest_bpm": 44,
                "weight_kg": 71.2,
                "power_source": "powermeter_4iiii",
                "source": "cp_model",
                "css_estimated": False,
                "note": None,
            }
        ],
    )

    (row,) = handlers.get_thresholds(fake_runner, max_rows=MAX_ROWS, discipline="bike")

    assert row["valid_from"] == "2026-03-01"
    assert row["valid_to"] == "infinity"
    assert row["css_estimated"] is False
    assert row["ftp_watts"] == 245.0
    assert row["weight_kg"] == 71.2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (0.30000000000000004, 0.3),
        (123.4567, 123.46),
        (7, 7.0),
    ],
)
def test_round_number_double_rounding(raw: Any, expected: float | None) -> None:
    assert handlers.round_number(raw) == expected


def test_parse_iso_date_rejects_garbage_naming_the_argument() -> None:
    with pytest.raises(ValueError, match="start_date"):
        handlers.parse_iso_date("not-a-date", "start_date")


def test_parse_iso_date_accepts_none() -> None:
    assert handlers.parse_iso_date(None, "start_date") is None
