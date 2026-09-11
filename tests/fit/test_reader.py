"""Tests of decode_fit on hand-built dicts (no binary fixture; see test_mapping.py
docstring for the convention).

Covers the elapsed_s monotonicity guard: garmin_fit_sdk record timestamps carry
sub-second precision that can round to a duplicate or backward elapsed_s once fed
through map_record's round((t - start_time).total_seconds()). Downstream
(t0.compute.regularization) treats a non-strictly-increasing elapsed_s sequence as a
caller error, not a gap -- so decode_fit must guarantee strictly increasing elapsed_s
itself, by dropping the offending sample (simplest possible fix, no merging/
re-timestamping).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from t0.fit.reader import decode_fit

if TYPE_CHECKING:
    import pytest

T0 = datetime(2026, 8, 1, 6, 0, 0, tzinfo=UTC)


def _msg(seconds: float, **fields: object) -> dict[str, object]:
    from datetime import timedelta

    return {"timestamp": T0 + timedelta(seconds=seconds), **fields}


def _messages(record_mesgs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "record_mesgs": record_mesgs,
        "session_mesgs": [{"start_time": T0}],
    }


def test_decode_fit_drops_sample_with_duplicate_elapsed_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Records at t=0.0s and t=0.4s both round to elapsed_s=0: the second is a
    # duplicate, not a real second sample -- must be dropped, not raise.
    record_mesgs = [
        _msg(0.0, heart_rate=120),
        _msg(0.4, heart_rate=121),
        _msg(1.0, heart_rate=122),
    ]
    monkeypatch.setattr("t0.fit.reader._decode_messages", lambda data: _messages(record_mesgs))

    result = decode_fit(b"irrelevant", activity_id=1)

    elapsed = [s.elapsed_s for s in result.samples]
    assert elapsed == [0, 1]
    # The kept sample at elapsed_s=0 is the first one seen, not a merge of the two.
    assert result.samples[0].heart_rate == 120


def test_decode_fit_drops_sample_with_backward_elapsed_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record whose rounded elapsed_s goes backward relative to the previous kept
    # sample (clock jitter) must also be dropped.
    record_mesgs = [
        _msg(0.0, heart_rate=120),
        _msg(2.0, heart_rate=121),
        _msg(1.9, heart_rate=122),  # rounds to elapsed_s=2, <= previous kept (2)
        _msg(3.0, heart_rate=123),
    ]
    monkeypatch.setattr("t0.fit.reader._decode_messages", lambda data: _messages(record_mesgs))

    result = decode_fit(b"irrelevant", activity_id=1)

    elapsed = [s.elapsed_s for s in result.samples]
    assert elapsed == [0, 2, 3]


def test_decode_fit_keeps_all_samples_when_already_strictly_increasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_mesgs = [_msg(0.0), _msg(1.0), _msg(2.0)]
    monkeypatch.setattr("t0.fit.reader._decode_messages", lambda data: _messages(record_mesgs))

    result = decode_fit(b"irrelevant", activity_id=1)

    assert [s.elapsed_s for s in result.samples] == [0, 1, 2]
