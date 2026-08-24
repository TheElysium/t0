"""Declarative catalog of daily-pollable ``garminconnect`` endpoints (batch L11).

``garminconnect.Garmin`` exposes about 130 methods, with no common signature convention --
some take a single day, others a range ``[start, end]``, still others return a list rather
than a day-aggregated object. Rather than a cascade of ``if`` by endpoint name (which this
module deliberately refuses), the catalog below follows the same principle as
``core.metric_method_priority`` (:mod:`garminator.load.method_priority`) : a data table,
a small handful of declared call shapes, and a pure function that translates them to
positional arguments.

The three shapes retained were verified by introspection of the installed library
(``inspect.signature(garminconnect.Garmin.<method>)``), never supposed from documentation --
this is exactly what ``tests/scheduler/test_catalog.py`` reverifies at each test run,
to detect any drift from a ``garminconnect`` update without ever calling Garmin.

This module does not import ``garminconnect`` : it describes a contract, it does not consume it.
Only the introspection test imports it (with ``skipif`` if absent).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ``date`` appears only in parameter annotation of `call_args` below --
    # never evaluated at runtime thanks to `from __future__ import annotations`. Import
    # under `TYPE_CHECKING`, no `noqa` : unlike the old comment claimed, no signature
    # in this module actually executes this type at runtime.
    from datetime import date


class CallShape(Enum):
    """Distinct call shapes observed among ``garminconnect.Garmin`` methods
    relevant for daily polling. See :func:`call_args` for their translation
    to positional arguments, and ``tests/scheduler/test_catalog.py`` for proof that
    each shape matches the real signature."""

    #: ``f(cdate: str) -> dict | list | None`` -- single day, aggregated.
    DAY = "day"
    #: ``f(startdate: str, enddate: str | None = None) -> dict | list`` -- a range ;
    #: called here with ``startdate == enddate == window_date`` to query only a
    #: single day, this API not offering a dedicated "single day" form.
    RANGE = "range"
    #: Same call shape as ``RANGE``, but result is a list of independent
    #: objects (one activity each) rather than a day-aggregated object -- handled separately
    #: because it's the only shape whose "scope_key" derives from the payload itself
    #: rather than always being ``None``. See
    #: :class:`garminator.scheduler.fetcher.GarminWindowFetcher`.
    ACTIVITIES_BY_DATE = "activities_by_date"


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    """A catalog entry: the exact name of the ``garminconnect.Garmin`` method
    (resolved dynamically by :meth:`garminator.garmin.client.GarminClient.fetch` via
    ``getattr``) and its declared call shape."""

    name: str
    call_shape: CallShape


#: Day-aggregated endpoints, call shape ``f(cdate: str)`` -- verified by introspection on
#: 2026-08-23 against ``garminconnect`` version installed in ``.venv``.
_DAY_ENDPOINTS: tuple[str, ...] = (
    "get_sleep_data",
    "get_stats",
    "get_user_summary",
    "get_hrv_data",
    "get_rhr_day",
    "get_stress_data",
    "get_all_day_stress",
    "get_respiration_data",
    "get_spo2_data",
    "get_max_metrics",
    "get_training_readiness",
    "get_training_status",
    "get_floors",
    "get_steps_data",
    "get_intensity_minutes_data",
    "get_body_battery_events",
    "get_daily_weigh_ins",
)

#: Range endpoints, call shape ``f(startdate: str, enddate: str | None = None)``.
_RANGE_ENDPOINTS: tuple[str, ...] = (
    "get_body_battery",
    "get_body_composition",
    "get_endurance_score",
    "get_hill_score",
)

#: Full catalog -- one entry per endpoint known to this batch.
CATALOG: tuple[EndpointSpec, ...] = (
    *(EndpointSpec(name, CallShape.DAY) for name in _DAY_ENDPOINTS),
    *(EndpointSpec(name, CallShape.RANGE) for name in _RANGE_ENDPOINTS),
    EndpointSpec("get_activities_by_date", CallShape.ACTIVITIES_BY_DATE),
)

#: Index by name, built once -- ``CATALOG`` remains the ordered source of truth
#: (useful for stable display), this index serves for point reads.
_CATALOG_BY_NAME: dict[str, EndpointSpec] = {spec.name: spec for spec in CATALOG}

#: Default subset relevant for a triathlete (see profil-athlete in project memory):
#: sleep, HRV, resting HR, stress, Body Battery, readiness, training status, body
#: composition, activities. Intentionally more restricted than entire ``CATALOG`` --
#: each endpoint added here multiplies network calls per run (see
#: ``DEFAULT_MAX_WINDOWS_PER_ENDPOINT`` in ``service.py``) ; an operator wanting the
#: full catalog passes it explicitly via ``--endpoint``.
#:
#: Body weight/composition -- trade-off DECIDED, do not reopen: ``get_body_composition``
#: is kept, ``get_daily_weigh_ins`` is not. Which actually carries data depends on how
#: weight enters Garmin (Index scale vs manual entry) ; the athlete explicitly indicated
#: that distinction was immaterial. We keep the conservative choice: an empty daily call
#: costs less than a history gap, and the second endpoint stays accessible via
#: ``--endpoint`` without code changes.
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "get_sleep_data",
    "get_hrv_data",
    "get_rhr_day",
    "get_stress_data",
    "get_body_battery",
    "get_training_readiness",
    "get_training_status",
    "get_body_composition",
    "get_max_metrics",
    "get_activities_by_date",
)


def is_known_endpoint(name: str) -> bool:
    """``True`` if ``name`` is declared in :data:`CATALOG`."""
    return name in _CATALOG_BY_NAME


def endpoint_spec(name: str) -> EndpointSpec:
    """The catalog entry for ``name``.

    Raises:
        ValueError: if ``name`` is not a known endpoint -- configuration error
            (typo by operator, or endpoint never added to catalog),
            not a silent case to absorb.
    """
    try:
        return _CATALOG_BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown endpoint in catalog: {name!r} -- declared endpoints: "
            f"{sorted(_CATALOG_BY_NAME)}"
        ) from None


def call_args(spec: EndpointSpec, window_date: date) -> tuple[str, ...]:
    """Translates ``window_date`` to positional arguments for the endpoint in ``spec``.

    - :attr:`CallShape.DAY` : single argument ``cdate``.
    - :attr:`CallShape.RANGE` and :attr:`CallShape.ACTIVITIES_BY_DATE` : identical
      ``startdate`` and ``enddate`` -- these two shapes offer no dedicated "single day"
      variant, the range reduced to a single date achieves the same effect.
    """
    iso = window_date.isoformat()
    if spec.call_shape is CallShape.DAY:
        return (iso,)
    return (iso, iso)
