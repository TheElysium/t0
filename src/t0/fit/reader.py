"""Decode FIT file to shared intermediate structure.

Only module in ``t0.fit`` touching bytes: rest (mapping, archive) works on
decoded dicts or already-verified file paths. This is the boundary from macro plan
("only terminal integration test needs real .FIT files").
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from garmin_fit_sdk import Decoder, Stream

from t0.fit.errors import FitParseError
from t0.fit.mapping import (
    derive_lap_index_for_lengths,
    map_lap,
    map_length,
    map_record,
    map_session,
)
from t0.fit.model import ParsedActivitySource

if TYPE_CHECKING:
    from pathlib import Path

    from t0.fit.model import SwimLengthRow


def _decode_messages(data: bytes) -> dict[str, Any]:
    """Decode raw FIT file bytes. SDK never raises: decode errors returned in list,
    surfaced here as :class:`FitParseError`."""
    try:
        stream = Stream.from_byte_array(bytearray(data))
        decoder = Decoder(stream)
        messages, errors = decoder.read()
    except Exception as exc:  # SDK can also raise directly on unusable stream
        raise FitParseError(f"FIT decoding impossible: {exc}") from exc
    if errors:
        raise FitParseError(
            "FIT decoding finished with errors, file rejected: " + "; ".join(str(e) for e in errors)
        )
    # garmin_fit_sdk.Decoder.read() returns SDK-specific TypedDict (FitMessages);
    # this module intentionally works in generic dict[str, Any] (see t0.fit.mapping,
    # must stay usable on any decoded dict, including hand-built in tests).
    # dict(messages) is plain Python dict at runtime: conversion zero-cost, lossless.
    return dict(messages)


def decode_fit(data: bytes, *, activity_id: int) -> ParsedActivitySource:
    """Decode FIT file (content already in memory) to :class:`ParsedActivitySource`.

    Args:
        data: raw ``.FIT`` file bytes.
        activity_id: Garmin activity identifier, provided by caller (FIT file doesn't
            contain it).

    Raises:
        FitParseError: truncated file, invalid CRC, or no usable ``record``/``session``
            messages. No side effects (decode is pure).
    """
    messages = _decode_messages(data)

    record_msgs: list[dict[str, Any]] = messages.get("record_mesgs", [])
    lap_msgs: list[dict[str, Any]] = messages.get("lap_mesgs", [])
    length_msgs: list[dict[str, Any]] = messages.get("length_mesgs", [])
    session_msgs: list[dict[str, Any]] = messages.get("session_mesgs", [])

    session = map_session(session_msgs[0]) if session_msgs else None

    start_time: datetime | None = None
    if session_msgs and isinstance(session_msgs[0].get("start_time"), datetime):
        start_time = session_msgs[0]["start_time"]
    elif record_msgs and isinstance(record_msgs[0].get("timestamp"), datetime):
        start_time = record_msgs[0]["timestamp"]

    if start_time is None and (record_msgs or lap_msgs):
        raise FitParseError(
            "Cannot determine activity start time "
            "(neither session.start_time nor record[0].timestamp)."
        )

    samples = [
        map_record(msg, activity_id=activity_id, start_time=start_time)  # type: ignore[arg-type]
        for msg in record_msgs
    ]
    laps = [map_lap(msg, activity_id=activity_id) for msg in lap_msgs]

    lap_index_by_length = derive_lap_index_for_lengths(lap_msgs)
    swim_lengths: list[SwimLengthRow | None] = []
    for msg in length_msgs:
        length_message_index = msg.get("message_index")
        lap_index = (
            lap_index_by_length.get(int(length_message_index))
            if length_message_index is not None
            else None
        )
        swim_lengths.append(map_length(msg, activity_id=activity_id, lap_index=lap_index))
    # Zero-duration glitch lengths are dropped at mapping (see map_length): filter
    # the None placeholders they left here.
    usable_lengths: list[SwimLengthRow] = [length for length in swim_lengths if length is not None]
    return ParsedActivitySource(
        source_format="fit",
        samples=samples,
        laps=laps,
        swim_lengths=usable_lengths,
        session=session,
        start_time=start_time,
    )


def decode_fit_file(path: Path, *, activity_id: int) -> ParsedActivitySource:
    """Variant of :func:`decode_fit` that reads file from disk."""
    return decode_fit(path.read_bytes(), activity_id=activity_id)
