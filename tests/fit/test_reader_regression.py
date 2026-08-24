"""Non-regression: decoding of the athlete's real FIT files, when present.

These files (``running.fit``, ``swim.fit``) hold GPS traces that reveal the athlete's
home address: they are excluded from the repository (see .gitignore) and must NEVER be
copied into it, ``tests/fixtures/`` included. This test reads them straight from the
repository root when they are locally present, and skips cleanly (``pytest.skip``,
never a failure) when they are absent -- which is the case on every machine other than
the athlete's, CI included.

To enable: drop ``running.fit`` and/or ``swim.fit`` at the repository root (next to
``pyproject.toml``). Nothing else to configure -- no environment variable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garminator.fit.reader import decode_fit_file

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNING_FIT = _REPO_ROOT / "running.fit"
_SWIM_FIT = _REPO_ROOT / "swim.fit"


def test_decode_running_fit_if_present() -> None:
    if not _RUNNING_FIT.exists():
        pytest.skip(
            "running.fit is absent (expected: the athlete's real file, unversioned). "
            "Drop it at the repository root to enable this test."
        )

    result = decode_fit_file(_RUNNING_FIT, activity_id=1)

    assert result.source_format == "fit"
    assert len(result.samples) > 0
    assert result.start_time is not None
    # Running Power is native: at least one non-null power value.
    assert any(s.power is not None for s in result.samples)


def test_decode_swim_fit_if_present() -> None:
    if not _SWIM_FIT.exists():
        pytest.skip(
            "swim.fit is absent (expected: the athlete's real file, unversioned). "
            "Drop it at the repository root to enable this test."
        )

    result = decode_fit_file(_SWIM_FIT, activity_id=2)

    assert result.source_format == "fit"
    # Swim data lives in the lengths, not in the record messages.
    assert len(result.swim_lengths) > 0
    assert any(length.total_strokes is not None for length in result.swim_lengths)
    # At least one length must attach to a lap via first_length_index/num_lengths.
    assert any(length.lap_index is not None for length in result.swim_lengths)

    # Active and idle lengths are distinguished: a pool swim file holds both. No count
    # is frozen here -- the athlete's real volumes must not appear in the repository.
    types = {length.length_type for length in result.swim_lengths}
    assert types == {"active", "idle"}
    # Idle lengths are rest periods: no stroke style, no stroke count. Those fields
    # stay None -- never set to 0.
    idle = [length for length in result.swim_lengths if length.length_type == "idle"]
    assert all(length.swim_stroke is None for length in idle)
    assert all(length.total_strokes is None for length in idle)
    # The swim_length_dur_pos constraint (migrations/004) requires duration_s > 0.
    assert all(length.duration_s > 0 for length in result.swim_lengths)
    # In swimming no record carries power/cadence/speed: those columns must stay NULL,
    # not 0.
    assert all(s.power is None for s in result.samples)
    assert all(s.speed_mps is None for s in result.samples)
