"""Multisport split (2026-09-05 triathlon shape): sessions -> leg bundles.

What these tests protect is what nothing else does: Garmin never publishes the legs
(VERIFIED, see t0.transform.multisport docstring), so a regression here empties the
dashboards on every race day with no red light -- the exact defect this batch fixes.
100% synthetic data, per this layer's mandate: no real FIT bytes. Times are
``conftest.START``-anchored, compressed from the real event (8 008 s) to round values.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from t0.fit.model import LapRow, SessionSummary, SwimLengthRow
from t0.transform.multisport import build_leg_bundle, split_multisport_legs

from .conftest import (
    ALL_THRESHOLD,
    ATHLETE_ID,
    BIKE_THRESHOLD,
    START,
    make_sample,
    make_session,
    multisport_context,
    parsed_source,
)

LEG_ID = 9200000000000000001
#: Session starts relative to conftest.START: swim 0 / T1 1341 / bike 1614 / T2 5677 /
#: run 5789, contiguous -- the VERIFIED triathlon file shape.
SESSION_STARTS = (0, 1341, 1614, 5677, 5789)
SESSION_DURATIONS = (1341.4, 272.9, 4062.6, 111.9, 2219.2)


def _triathlon_sessions() -> tuple[SessionSummary, ...]:
    return tuple(
        make_session(
            START + timedelta(seconds=start_s),
            sport=sport,
            sub_sport=sub_sport,
            duration_s=duration,
            **({"distance_m": 39033.6} if sport == "cycling" else {}),
        )
        for start_s, sport, sub_sport, duration in zip(
            SESSION_STARTS,
            ("swimming", "transition", "cycling", "transition", "running"),
            ("open_water", "generic", "generic", "generic", "generic"),
            SESSION_DURATIONS,
            strict=True,
        )
    )


def test_split_produces_one_leg_per_session_in_chronological_order() -> None:
    decoded = parsed_source([], sessions=_triathlon_sessions())
    plans = split_multisport_legs(decoded, multisport_context())

    assert [(p.leg_order, p.sport_code) for p in plans] == [
        (1, "open_water"),
        (2, "transition"),
        (3, "cycling"),
        (4, "transition"),
        (5, "running"),
    ]
    assert [p.discipline for p in plans] == ["swim", "other", "bike", "other", "run"]
    # Session offsets from file start, re-derived not guessed.
    assert [p.offset_s for p in plans] == list(SESSION_STARTS)
    assert [p.duration_s for p in plans] == [1341, 273, 4063, 112, 2219]


def test_every_sample_is_assigned_exactly_once_and_rebased() -> None:
    decoded = parsed_source(
        [make_sample(second, heart_rate=150) for second in range(0, 7000, 10)],
        sessions=_triathlon_sessions(),
    )
    plans = split_multisport_legs(decoded, multisport_context())

    assigned = [s for p in plans for s in p.samples]
    assert len(assigned) == len(decoded.samples)
    # Re-based: a leg starts at 0, never at 5 789 s into the event.
    assert all(s.elapsed_s >= 0 for p in plans for s in p.samples)
    # First run sample at file-second 5790, window opens at 5789 -> elapsed 1.
    assert plans[-1].samples[0].elapsed_s == 1


def test_cumulative_distance_is_rebased_per_leg() -> None:
    """FIT 'distance' is cumulative over the whole FILE, not per session (VERIFIED:
    the 2026-09-05 run leg's last sample carried ~50 km, the event total). Without
    re-basing, every leg after the first would claim the event's running total."""
    sessions = (
        make_session(START, sport="swimming", sub_sport="open_water", duration_s=600.0),
        make_session(
            START + timedelta(seconds=600), sport="running", duration_s=600.0
        ),
    )
    samples = [
        make_sample(0, heart_rate=150, distance_m=0.0),
        make_sample(300, heart_rate=150, distance_m=300.0),
        make_sample(599, heart_rate=150, distance_m=590.0),
        make_sample(600, heart_rate=150, distance_m=590.0),
        make_sample(1199, heart_rate=150, distance_m=1100.0),
    ]
    decoded = parsed_source(samples, sessions=sessions)
    plans = split_multisport_legs(decoded, multisport_context())

    run = plans[1]
    # Last run sample: 1100 - 590 (baseline at T1's end) = 510 m, not 1100.
    assert run.samples[-1].distance_m == 510.0
    assert run.samples[0].distance_m == 0.0
    # First leg untouched (baseline 0).
    assert plans[0].samples[-1].distance_m == 590.0


def test_sample_at_a_boundary_goes_to_the_later_leg() -> None:
    """Half-open windows: the sample AT T1's start belongs to T1, not to the swim."""
    decoded = parsed_source(
        [make_sample(1341, heart_rate=150), make_sample(1340, heart_rate=150)],
        sessions=_triathlon_sessions(),
    )
    plans = split_multisport_legs(decoded, multisport_context())

    owners = [p.sport_code for p in plans if p.samples]
    assert owners == ["open_water", "transition"]


def test_last_session_never_cut_on_the_right_edge() -> None:
    """Samples past the last timer end (cool-down still recording): data, not garbage."""
    decoded = parsed_source(
        [make_sample(second, heart_rate=150) for second in range(5789, 9000, 10)],
        sessions=_triathlon_sessions(),
    )
    plans = split_multisport_legs(decoded, multisport_context())

    assert len(plans[-1].samples) == len(range(5789, 9000, 10))
    assert not any(p.samples for p in plans[:-1])


def test_unknown_session_sport_raises_naming_the_pair() -> None:
    sessions = (
        make_session(START, sport="swimming", sub_sport="open_water", duration_s=600.0),
        make_session(START + timedelta(seconds=600), sport="curling", duration_s=600.0),
    )
    decoded = parsed_source([], sessions=sessions)

    with pytest.raises(ValueError, match="curling"):
        split_multisport_legs(decoded, multisport_context())


def test_session_without_start_time_raises() -> None:
    sessions = (
        make_session(START, sport="swimming", duration_s=600.0),
        make_session(START + timedelta(seconds=600), sport="running", duration_s=600.0),
    )
    stripped = replace(sessions[1])
    object.__setattr__(
        stripped, "raw", {k: v for k, v in stripped.raw.items() if k != "start_time"}
    )
    decoded = parsed_source([], sessions=(sessions[0], stripped))

    with pytest.raises(ValueError, match="start_time"):
        split_multisport_legs(decoded, multisport_context())


def test_zero_duration_session_raises() -> None:
    sessions = (
        make_session(START, sport="swimming", duration_s=600.0),
        make_session(START + timedelta(seconds=600), sport="running", duration_s=0.0),
    )
    decoded = parsed_source([], sessions=sessions)

    with pytest.raises(ValueError, match="<= 0"):
        split_multisport_legs(decoded, multisport_context())


def test_sample_before_first_session_raises() -> None:
    sessions = (
        make_session(START + timedelta(seconds=100), sport="running", duration_s=600.0),
        make_session(START + timedelta(seconds=700), sport="cycling", duration_s=600.0),
    )
    decoded = parsed_source([make_sample(0, heart_rate=150)], sessions=sessions)

    with pytest.raises(ValueError, match="before the first session"):
        split_multisport_legs(decoded, multisport_context())


def test_single_session_file_is_never_split() -> None:
    decoded = parsed_source(
        [make_sample(second, heart_rate=150) for second in range(600)],
        sessions=(make_session(START, sport="running", duration_s=600.0),),
    )

    assert split_multisport_legs(decoded, multisport_context()) == ()


def test_laps_and_lengths_follow_their_session() -> None:
    sessions = _triathlon_sessions()
    laps = [
        LapRow(activity_id=1, lap_index=0, start_time=START, duration_s=600),
        LapRow(
            activity_id=1,
            lap_index=1,
            start_time=START + timedelta(seconds=5789),
            duration_s=600,
        ),
    ]
    lengths = [
        # A length inside the FIRST session -- attachment rule is what is under test,
        # not open-water realism (an OW swim carries no length message).
        SwimLengthRow(
            activity_id=1,
            length_index=0,
            start_time=START + timedelta(seconds=10),
            duration_s=25.0,
            length_type="active",
        ),
    ]
    decoded = parsed_source([], laps=laps, swim_lengths=lengths, sessions=sessions)
    plans = split_multisport_legs(decoded, multisport_context())

    assert plans[0].laps
    assert plans[0].swim_lengths
    assert plans[-1].laps
    assert not plans[-1].swim_lengths


def test_leg_bundle_goes_through_the_standard_pipeline() -> None:
    """One leg builds like any activity: own id, parent linkage, leg_order, source."""
    decoded = parsed_source([], sessions=_triathlon_sessions())
    parent = multisport_context()
    plans = split_multisport_legs(decoded, parent)

    bundle = build_leg_bundle(
        plans[2],  # bike
        leg_activity_id=9200000000000000001,
        parent_context=parent,
        athlete_id=ATHLETE_ID,
        fit_file_id=42,
        threshold=BIKE_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )
    row = bundle.activity
    assert row.activity_id == 9200000000000000001
    assert row.sport_code == "cycling"
    assert row.parent_activity_id == parent.activity_id
    assert row.leg_order == 3
    assert row.is_parent is False
    assert row.source == "leg"
    assert row.duration_s == 4063
    # No samples in the leg: distance comes from the session fallback (cf.
    # ActivityContext.distance_m contract).
    assert row.distance_m == pytest.approx(39033.6)
    assert row.start_time == START + timedelta(seconds=1614)


def test_leg_metrics_are_per_leg_and_load_is_computable() -> None:
    samples = [
        make_sample(second, heart_rate=150)
        for second in list(range(0, 1341, 60))
        + list(range(1341, 1614, 10))
        + list(range(5677, 5789, 10))
        + list(range(5789, 8000, 60))
    ]
    decoded = parsed_source(samples, sessions=_triathlon_sessions())
    plans = split_multisport_legs(decoded, multisport_context())

    run = plans[-1]
    bundle = build_leg_bundle(
        run,
        leg_activity_id=9200000000000000001,
        parent_context=multisport_context(),
        athlete_id=ATHLETE_ID,
        fit_file_id=42,
        threshold=ALL_THRESHOLD,
        all_threshold=ALL_THRESHOLD,
        sex="male",
    )
    assert bundle.activity.avg_hr == 150
    assert bundle.activity.max_hr == 150
    assert bundle.activity.leg_order == 5
    assert bundle.hr_histogram
    # hrTSS is computable on the leg: the load views will find it.
    tss = [m for m in bundle.metrics if m.metric == "tss" and m.method_code == "hr_hrtss"]
    assert tss
    assert tss[0].value is not None
    assert tss[0].value > 0
