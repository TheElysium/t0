"""Adapter between :mod:`garminator.garmin.client` and the ``WindowFetcher`` of
:mod:`garminator.scheduler.service` (batch L11).

``run_scheduled_poll`` imports neither ``GarminClient`` nor the catalog -- it knows
only the ``Protocol WindowFetcher``. This module closes the loop: it consults
:mod:`garminator.scheduler.catalog` to learn how to call a given endpoint, invokes
:meth:`~garminator.garmin.client.GarminClient.fetch` (which handles rate limit, backoff,
and writes to ``raw.api_response`` itself), then translates the obtained payload into
``Sequence[FetchedWindow]``.

No defensive ``try/except`` here, deliberately : ``run_scheduled_poll`` already captures
any exception raised by the injected ``WindowFetcher`` and marks the window ``failed`` --
adding one here would only mask the real error (network, authentication,
unknown endpoint) behind a misleading "succeeded with zero payload" state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from garminator.garmin.raw_store import payload_sha256
from garminator.scheduler.catalog import CallShape, call_args, endpoint_spec
from garminator.scheduler.service import FetchedWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from garminator.garmin.client import GarminClient, IngestRunTracker


def _validate_activities_payload(payload: object, window_date: date) -> list[dict[str, object]]:
    """Validates the shape of ``get_activities_by_date`` payload before extracting
    ``activityId`` from it -- raises a MEANINGFUL error rather than letting indexing fail,
    whose raw message would end up as-is in ``raw.poll_window.last_error`` (unreadable
    to operator).

    Two real malformed shapes, observed upstream of this batch:

    1. Garmin sometimes answers with an error dict in HTTP 200 (e.g.
       ``{"errorMessage": "..."}``) rather than the expected list: truthy, so not
       absorbed by "empty payload" case -- without this validation, iterating over it
       would yield ``str`` (dict keys) and ``activity["activityId"]`` would raise
       ``TypeError: string indices must be integers``, a message saying nothing about
       the window in question.
    2. An activity in the list without ``activityId`` key: without validation,
       ``activity["activityId"]`` raises ``KeyError: 'activityId'`` -- once stringified
       by ``run_scheduled_poll`` (``str(exc)``), this message is literally just
       ``'activityId'``.

    Deliberately NO ``try/except`` around the network call itself (see module docstring) :
    this function still raises, it just raises a meaningful message rather than opaque
    ``KeyError``/``TypeError`` -- the window is still marked ``failed`` by
    ``run_scheduled_poll``, and remains replayable.
    """
    if not payload:
        return []
    if not isinstance(payload, list):
        keys_hint = f" (keys: {sorted(payload.keys())!r})" if isinstance(payload, dict) else ""
        raise ValueError(
            f"get_activities_by_date/{window_date.isoformat()} : unexpected payload -- "
            f"list expected, {type(payload).__name__} received{keys_hint}."
        )
    activities: list[dict[str, object]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(
                f"get_activities_by_date/{window_date.isoformat()} : element {index} "
                f"unexpected -- dict expected, {type(item).__name__} received."
            )
        if "activityId" not in item:
            raise ValueError(
                f"get_activities_by_date/{window_date.isoformat()} : element {index} without "
                f"'activityId' key -- present keys: {sorted(item.keys())!r}."
            )
        activities.append(item)
    return activities


@dataclass(slots=True)
class GarminWindowFetcher:
    """Real ``WindowFetcher`` : actually polls Garmin via a :class:`GarminClient`.

    ``tracker`` is optional to remain usable outside an ``ingest_run(...)`` (e.g.
    an ad-hoc operator script) -- ``GarminClient.fetch`` already accepts ``tracker=None``.

    Design decision -- tracing individual activities in raw (see batch report for full
    discussion) : ``GarminClient.fetch`` is called **exactly once** per window, regardless
    of endpoint, never once per activity.

    The TRUE reason, sole and sufficient : ``get_activities_by_date`` already returns
    all day's activities in a single call. Calling ``client.fetch`` once per activity
    (to get one ``raw.api_response`` row per activity, with ``scope_key = activityId``)
    would explode request volume against a non-public Garmin API -- especially on a
    current backfill from 2025-04-03, where each day can carry multiple activities
    (multisport, brick). This is NOT a module boundary question : this batch's entry point
    already imports ``ingest_run`` from ``client.py``, and ``activity_files.py`` (batch L12)
    already writes directly to another raw table (``raw.fit_file``) -- "only one module
    writes raw" was never a repo rule.

    So the single ``raw.api_response`` row carries ``scope_key=None`` and contains
    the entire list as Garmin returned it -- nothing lost from raw storage standpoint.
    Activity discrimination (``scope_key`` = ``activityId``) exists only at the
    ``raw.payload_seen`` level, via the ``FetchedWindow`` returned here: this is the
    exact level ``run_scheduled_poll`` needs to detect that a specific activity changed
    content from one run to the next, without duplicating raw storage or multiplying requests.

    Real limit assumed, burden of a future transform batch: ``payload_seen`` can say
    THAT an activity changed (its ``payload_sha256`` changed), but finding the raw
    ``raw.api_response`` row requires ``jsonb`` filtering on ``(endpoint, scope_date)``
    then searching the ``activityId`` in the JSON array -- the partial index
    ``api_response_scope_key_idx ... WHERE scope_key IS NOT NULL`` (``migrations/002_raw.sql``)
    stays unused for this endpoint, since the single row carries ``scope_key=None``.
    A future batch needing this index must either accept it (daily volume stays small)
    or revisit this decision.
    """

    client: GarminClient
    tracker: IngestRunTracker | None = None

    def __call__(self, endpoint: str, window_date: date) -> Sequence[FetchedWindow]:
        spec = endpoint_spec(endpoint)
        args = call_args(spec, window_date)
        payload = self.client.fetch(
            endpoint, *args, scope_date=window_date, scope_key=None, tracker=self.tracker
        )

        if spec.call_shape is CallShape.ACTIVITIES_BY_DATE:
            activities = _validate_activities_payload(payload, window_date)
            return [
                FetchedWindow(
                    scope_key=str(activity["activityId"]),
                    payload_sha256=payload_sha256(activity),
                )
                for activity in activities
            ]

        # Day with no data (rest without worn device, endpoint not relevant today,
        # ...) : Garmin answers with ``None``, ``{}``, or ``[]`` depending on endpoint.
        # This is not an error -- an empty sequence lets ``run_scheduled_poll`` mark
        # the window ``done`` without recording anything in ``raw.payload_seen``.
        if not payload:
            return []
        return [FetchedWindow(scope_key=None, payload_sha256=payload_sha256(payload))]
