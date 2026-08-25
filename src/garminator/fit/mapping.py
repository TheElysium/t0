"""Declarative mapping: decoded FIT message → ``core`` column.

Design constraint (macro plan, L3): "FIT message to column mapping driven by lookup
table and tested on decoded messages (dicts), not bytes". Functions never take file path
or bytes—only dicts from ``garmin_fit_sdk.Decoder.read()`` (or hand-built test dict).

Any value uncovered by :class:`FieldSpec` goes to ``extra`` column—nothing silently lost,
per ``core.sample.extra`` comment (migrations/005_core_series.sql).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from garminator.fit.errors import FitParseError
from garminator.fit.model import LapRow, SampleRow, SessionSummary, SwimLengthRow

#: Conversion factor from FIT "semicircles" (signed 32-bit int) to decimal degrees.
_SEMICIRCLE_TO_DEG = 180.0 / (2**31)


def semicircles_to_degrees(value: Any) -> float | None:
    """Convert FIT semicircles coordinate to decimal degrees."""
    if value is None:
        return None
    return float(value) * _SEMICIRCLE_TO_DEG


def round_to_int(value: Any) -> int | None:
    """Round FIT duration (float seconds, ~1 ms resolution) to integer seconds."""
    if value is None:
        return None
    return round(float(value))


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Target column, its possible sources by preference order, and converter.

    ``sources`` is sequence to cover case where same physical field has two FIT names
    (e.g. ``enhanced_altitude`` supersedes ``altitude`` but not always present): first
    populated source in message wins.
    """

    column: str
    sources: Sequence[str]
    converter: Callable[[Any], Any] | None = None


#: Mapping table ``record`` (mesg num 20) → ``core.sample``.
RECORD_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("latitude_deg", ("position_lat",), semicircles_to_degrees),
    FieldSpec("longitude_deg", ("position_long",), semicircles_to_degrees),
    FieldSpec("altitude_m", ("enhanced_altitude", "altitude")),
    FieldSpec("distance_m", ("distance",)),
    FieldSpec("speed_mps", ("enhanced_speed", "speed")),
    FieldSpec("heart_rate", ("heart_rate",)),
    FieldSpec("cadence", ("cadence",)),
    FieldSpec("power", ("power",)),
    FieldSpec("temperature", ("temperature",)),
    FieldSpec("vertical_oscillation", ("vertical_oscillation",)),
    FieldSpec("stance_time", ("stance_time",)),
    FieldSpec("step_length", ("step_length",)),
    FieldSpec("vertical_ratio", ("vertical_ratio",)),
)

#: Mapping table ``lap`` (mesg num 19) → ``core.activity_lap``.
#: ``np_watts`` / ``np_convention`` intentionally absent: see
#: :class:`garminator.fit.model.LapRow` docstring.
LAP_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("elapsed_s", ("total_elapsed_time",), round_to_int),
    FieldSpec("distance_m", ("total_distance",)),
    FieldSpec("avg_speed_mps", ("enhanced_avg_speed", "avg_speed")),
    FieldSpec("avg_hr", ("avg_heart_rate",)),
    FieldSpec("max_hr", ("max_heart_rate",)),
    FieldSpec("avg_power", ("avg_power",)),
    FieldSpec("max_power", ("max_power",)),
    FieldSpec("avg_cadence", ("avg_cadence",)),
    FieldSpec("ascent_m", ("total_ascent",)),
    FieldSpec("descent_m", ("total_descent",)),
    FieldSpec("intensity", ("intensity",)),
    FieldSpec("lap_trigger", ("lap_trigger",)),
    FieldSpec("swim_stroke", ("swim_stroke",)),
    FieldSpec("num_lengths", ("num_lengths",)),
)

#: Mapping table ``length`` (mesg num 101) → ``core.swim_length``.
LENGTH_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("swim_stroke", ("swim_stroke",)),
    FieldSpec("total_strokes", ("total_strokes",)),
    FieldSpec("avg_cadence", ("avg_swimming_cadence",)),
)

#: ``session`` message fields kept as COMPARISON metrics, never as truth source
#: (research/05-postgres-schema.md §8.1, addendum §1).
SESSION_COMPARISON_FIELDS: tuple[str, ...] = (
    "training_load_peak",
    "total_training_effect",
    "total_anaerobic_training_effect",
    "workout_rpe",
    "workout_feel",
    "training_stress_score",
)


def _apply_specs(
    msg: dict[str, Any], specs: tuple[FieldSpec, ...]
) -> tuple[dict[str, Any], set[str]]:
    """Apply :class:`FieldSpec` table to decoded message.

    Returns mapped values and set of consumed source keys (so caller can build ``extra``
    from remainder without duplication).
    """
    mapped: dict[str, Any] = {}
    consumed: set[str] = set()
    for spec in specs:
        value = None
        # All sources for a spec are "covered" by mapping table, even those losing
        # preference (e.g. "altitude" vs "enhanced_altitude"): else rejected source
        # leaks into `extra` despite being well-known.
        consumed.update(spec.sources)
        for source in spec.sources:
            candidate = msg.get(source)
            if candidate is not None:
                value = candidate
                break
        mapped[spec.column] = spec.converter(value) if spec.converter else value
    return mapped, consumed


def _jsonable(value: Any) -> Any:
    """Coerce a decoded FIT value into something ``psycopg.types.json.Jsonb`` accepts.

    ``extra`` is written to a ``jsonb`` column, and its contract is that NOTHING is
    silently lost -- so an unserializable value must be converted, never dropped. Two
    types come out of the FIT decoder that ``json`` cannot encode:

    * ``datetime`` (and its ``date``/``time`` relatives). A ``lap`` message carries a
      ``timestamp`` field, uncovered by ``LAP_FIELD_SPECS`` because it duplicates the
      lap END instant already derivable from ``start_time + elapsed_s``. Left as-is it
      raised ``TypeError: Object of type datetime is not JSON serializable`` and failed
      the WHOLE file -- observed on the athlete's real running and swimming files.
      ISO 8601 keeps the value readable and round-trippable.
    * ``bytes``, emitted for developer/manufacturer-specific fields absent from the
      profile. Rendered as hex for the same reason.

    Containers are walked, because a FIT field can be an array of either.
    """
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _build_extra(msg: dict[str, Any], consumed: set[str], *, exclude: set[str]) -> dict[str, Any]:
    """Remaining message fields uncovered by mapping table.

    Keys forced to ``str``: decoder falls back to numeric field identifiers (``int``)
    for fields missing from known profile (see addendum); ``jsonb`` accepts text keys only.
    Values go through :func:`_jsonable` for the symmetric reason on the value side.
    """
    return {
        str(key): _jsonable(value)
        for key, value in msg.items()
        if key not in consumed and key not in exclude
    }


def map_record(msg: dict[str, Any], *, activity_id: int, start_time: datetime) -> SampleRow:
    """Convert decoded ``record`` message to :class:`SampleRow`.

    Raises:
        FitParseError: if ``timestamp`` absent—sample without timestamp cannot be
            keyed into series by ``(activity_id, t)``.
    """
    t = msg.get("timestamp")
    if not isinstance(t, datetime):
        raise FitParseError("Record message missing usable timestamp: row rejected.")
    mapped, consumed = _apply_specs(msg, RECORD_FIELD_SPECS)
    extra = _build_extra(msg, consumed, exclude={"timestamp"})
    elapsed_s = round((t - start_time).total_seconds())
    return SampleRow(activity_id=activity_id, t=t, elapsed_s=elapsed_s, extra=extra, **mapped)


def map_lap(msg: dict[str, Any], *, activity_id: int) -> LapRow:
    """Convert decoded ``lap`` message to :class:`LapRow`.

    ``lap_index`` comes from ``message_index``: native order from device, never
    recalculated—fabricated index would mask missing lap in FIT stream.

    Raises:
        FitParseError: if ``start_time`` or ``total_timer_time`` (lap duration) missing.
            ``core.activity_lap.duration_s`` is ``NOT NULL``: lap without duration not
            insertable row; guessing value worse than rejection.
    """
    start_time = msg.get("start_time")
    if not isinstance(start_time, datetime):
        raise FitParseError("Lap message missing usable start_time: row rejected.")
    duration_s = round_to_int(msg.get("total_timer_time"))
    if duration_s is None:
        raise FitParseError("Lap message missing total_timer_time: duration not deducible.")
    lap_index = msg.get("message_index")
    if lap_index is None:
        raise FitParseError("Lap message missing message_index: lap index not deducible.")
    mapped, consumed = _apply_specs(msg, LAP_FIELD_SPECS)
    extra = _build_extra(msg, consumed, exclude={"start_time", "total_timer_time", "message_index"})
    return LapRow(
        activity_id=activity_id,
        lap_index=int(lap_index),
        start_time=start_time,
        duration_s=duration_s,
        extra=extra,
        **mapped,
    )


def map_length(msg: dict[str, Any], *, activity_id: int, lap_index: int | None) -> SwimLengthRow:
    """Convert decoded ``length`` message to :class:`SwimLengthRow`.

    ``duration_s`` NOT rounded (unlike ``lap``): column is ``real`` to preserve FIT
    centisecond precision (migration 004 comment). ``lap_index`` provided by caller: isolated
    ``length`` message doesn't carry its lap attachment (see :func:`derive_lap_index_for_lengths`).
    """
    start_time = msg.get("start_time")
    if not isinstance(start_time, datetime):
        raise FitParseError("Length message missing usable start_time: row rejected.")
    duration = msg.get("total_timer_time", msg.get("total_elapsed_time"))
    if duration is None:
        raise FitParseError("Length message missing usable duration: row rejected.")
    length_type = msg.get("length_type")
    if length_type not in ("active", "idle"):
        raise FitParseError(
            f"Length message with unexpected length_type ({length_type!r}): "
            "only 'active'/'idle' accepted by core.swim_length."
        )
    length_index = msg.get("message_index")
    if length_index is None:
        raise FitParseError("Length message missing message_index: index not deducible.")
    mapped, consumed = _apply_specs(msg, LENGTH_FIELD_SPECS)
    return SwimLengthRow(
        activity_id=activity_id,
        length_index=int(length_index),
        lap_index=lap_index,
        start_time=start_time,
        duration_s=float(duration),
        length_type=length_type,
        **mapped,
    )


def derive_lap_index_for_lengths(
    lap_msgs: Sequence[dict[str, Any]],
) -> dict[int, int]:
    """Reconstruct for each ``length`` message_index its lap message_index.

    FIT ``length`` messages don't carry their lap attachment. Swim session ``lap``
    messages have ``first_length_index`` and ``num_lengths`` (verified on ``swim.fit``),
    delimiting contiguous length index range. This relation used, not naive counting:
    survives lap with zero lengths (``num_lengths==0``, observed on manual rests).

    Returns:
        Dict ``{length_message_index: lap_message_index}``. Length_index uncovered by
        any lap range absent from result (caller treats ``lap_index`` as ``None`` rather
        than inventing attachment).
    """
    result: dict[int, int] = {}
    for lap in lap_msgs:
        first = lap.get("first_length_index")
        count = lap.get("num_lengths")
        lap_index = lap.get("message_index")
        if first is None or count is None or lap_index is None:
            continue
        for length_index in range(int(first), int(first) + int(count)):
            result[length_index] = int(lap_index)
    return result


def map_session(msg: dict[str, Any]) -> SessionSummary:
    """Extract comparison fields from decoded ``session`` message.

    Never raises: incomplete ``session`` or missing comparison fields normal (e.g.
    ``training_stress_score`` absent from both real FITs, addendum §1), yields ``None``,
    never exception.
    """
    values = {field_name: msg.get(field_name) for field_name in SESSION_COMPARISON_FIELDS}
    return SessionSummary(raw=dict(msg), **values)
