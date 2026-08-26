"""HTTP endpoint for manual RPE entry (POST ``/activities/{activity_id}/rpe``).

Closes the RPE loop: an activity with neither usable power nor HR gets
``core.activity.needs_manual_rpe = true`` and a NULL canonical TSS forever, because no
computed method can produce a value and no path existed to enter one by hand. This
module adds that path, LOCAL access only (same documented product decision as
:mod:`garminator.api.upload`: no Ingress, no TLS, no authentication).

WHAT IS WRITTEN, AND ON WHICH SCALE. One ``core.activity_metric`` row
``(metric='tss', method_code='rpe')`` whose ``value`` is the RAW RPE (1-10), NOT a
TSS-scale number. This is not an approximation: ``core.load_method`` seeds the ``rpe``
method with ``scale = 'rpe'`` (migrations/003_core_ref.sql), so every load view that
sums TSS joins on ``scale = 'tss'`` and structurally excludes this row — the raw score
can therefore never pollute CTL/ATL while remaining visible in wide views and available
as the canonical value for activities nothing else could measure.

CANONICAL INVARIANT. The partial unique index ``activity_metric_canonical_uq ON
(activity_id, metric) WHERE is_canonical`` forbids two canonical rows. Behavior:

- Activity unknown -> :class:`~garminator.api.errors.ActivityNotFoundError` -> 404,
  before any write.
- A canonical TSS from a MEASURED method already exists and ``force`` is absent ->
  :class:`~garminator.api.errors.CanonicalTssConflictError` -> 409: entering RPE would
  silently override measured data.
- With ``force: true``: the measured row is DEMOTED (``is_canonical = false``, never
  deleted — the measured value stays comparable) in the same transaction, BEFORE the
  RPE row is inserted canonical, so the index invariant holds at every instant.
- Otherwise (no canonical TSS, or canonical TSS already ``rpe``): the previous RPE row
  is deleted then reinserted — idempotent overwrite, re-posting replaces the athlete's
  previous entry.

``needs_manual_rpe`` is flipped to ``false`` in the same transaction once a canonical
value exists: the backlog query (partial index ``activity_needs_rpe_idx``) must stop
listing the activity the moment its loop is closed.

Error -> HTTP status mapping, same deliberate explicitness as :mod:`garminator.api.upload`:

- :class:`~garminator.api.errors.ActivityNotFoundError` -> 404.
- :class:`~garminator.api.errors.CanonicalTssConflictError` -> 409 (conflict).
- Body validation (RPE outside 1-10, non-integer) -> 422 via pydantic.
- Any other exception: allowed to propagate (FastAPI default 500).

NOTE ON REUSE. There is NO existing Python computation to delegate to:
``garminator.transform.manual`` derives sport/duration/timezone for manual FILE uploads
and ``garminator.load.activity_load`` explicitly does not emit ``rpe`` rows ("manual
entry" per its own docstring). The RPE value IS the athlete's input — there is nothing
to compute, only a validated write through the store conventions of
:mod:`garminator.transform.store` (structural ``DBConnection`` protocol, single
transaction, one commit at the end), reused here without reinventing them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from garminator.api.errors import ActivityNotFoundError, CanonicalTssConflictError
from garminator.api.upload import get_upload_db_conn

# Runtime import, never TYPE_CHECKING: same rule as garminator.api.upload (see its
# comment) -- FastAPI resolves annotations through get_type_hints(), and a name missing
# at runtime silently turns the parameter into an untyped query parameter.
from garminator.garmin.raw_store import DBConnection  # noqa: TC001

router = APIRouter(prefix="/activities", tags=["activities"])

#: ``core.load_method.method_code`` of the manually entered method (migrations/003).
RPE_METHOD_CODE = "rpe"

#: Metric the manual entry resolves: RPE terminates the TSS fallback cascade.
TSS_METRIC = "tss"


class RpeRequest(BaseModel):
    """Request body of a manual RPE entry.

    ``rpe`` is the athlete's rating of perceived exertion, integer 1-10 (Borg CR10
    convention). ``force`` is required only to override a canonical TSS from a measured
    method; see module docstring for what it does and does not destroy.
    """

    rpe: int = Field(ge=1, le=10)
    force: bool = False


class RpeResponse(BaseModel):
    """Post-write state of the activity, so the caller never has to re-read to confirm."""

    activity_id: int
    tss: float
    method_code: str
    needs_manual_rpe: bool


def _load_needs_manual_rpe(conn: DBConnection, activity_id: int) -> bool | None:
    """``core.activity.needs_manual_rpe``, or ``None`` when the activity is unknown."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT needs_manual_rpe FROM core.activity WHERE activity_id = %s",
            (activity_id,),
        )
        row = cur.fetchone()
    return None if row is None else bool(row[0])


def _canonical_tss_method_code(conn: DBConnection, activity_id: int) -> str | None:
    """``method_code`` of the activity's canonical TSS row, or ``None`` when absent.

    At most one such row exists by construction (partial unique index
    ``activity_metric_canonical_uq``): reading it back rather than counting keeps the
    conflict decision on the exact value that matters — which method won.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT method_code FROM core.activity_metric "
            "WHERE activity_id = %s AND metric = %s AND is_canonical",
            (activity_id, TSS_METRIC),
        )
        row = cur.fetchone()
    return None if row is None else str(row[0])


def record_manual_rpe(
    conn: DBConnection, *, activity_id: int, rpe: int, demote_measured: bool
) -> float:
    """Write the RPE row canonical and close the manual-entry flag, in ONE transaction.

    Order inside the transaction is not free: the demotion MUST precede the insert,
    otherwise two canonical rows would coexist for one instant and violate the partial
    unique index. The prior DELETE makes re-posting an overwrite, not a duplicate.
    Single ``commit()`` at the end, same convention as
    :func:`garminator.transform.store.store_activity_bundle`: a failure leaves nothing
    partial visible.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM core.activity_metric "
            "WHERE activity_id = %s AND metric = %s AND method_code = %s",
            (activity_id, TSS_METRIC, RPE_METHOD_CODE),
        )
        if demote_measured:
            cur.execute(
                "UPDATE core.activity_metric SET is_canonical = false "
                "WHERE activity_id = %s AND metric = %s AND is_canonical",
                (activity_id, TSS_METRIC),
            )
        # `threshold_id` stays NULL: no threshold takes part in an athlete-given score.
        cur.execute(
            "INSERT INTO core.activity_metric "
            "(activity_id, metric, method_code, value, is_canonical, threshold_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (activity_id, TSS_METRIC, RPE_METHOD_CODE, float(rpe), True, None),
        )
        cur.execute(
            "UPDATE core.activity SET needs_manual_rpe = false, updated_at = now() "
            "WHERE activity_id = %s",
            (activity_id,),
        )
    conn.commit()
    return float(rpe)


@router.post("/{activity_id}/rpe", response_model=RpeResponse)
async def enter_manual_rpe(
    activity_id: int,
    body: RpeRequest,
    conn: Annotated[DBConnection, Depends(get_upload_db_conn)],
) -> RpeResponse:
    """Enter a manual RPE for one activity; idempotent on re-posting."""
    needs_manual_rpe = _load_needs_manual_rpe(conn, activity_id)
    try:
        if needs_manual_rpe is None:
            raise ActivityNotFoundError(f"Unknown activity {activity_id}: no core.activity row.")
        canonical_method = _canonical_tss_method_code(conn, activity_id)
        demote_measured = False
        if canonical_method is not None and canonical_method != RPE_METHOD_CODE:
            if not body.force:
                raise CanonicalTssConflictError(
                    f"Activity {activity_id} already has a canonical TSS from measured "
                    f"method {canonical_method!r}: posting an RPE would silently override "
                    'measured data. Repeat with {"force": true} to demote it deliberately.'
                )
            demote_measured = True
        tss = record_manual_rpe(
            conn, activity_id=activity_id, rpe=body.rpe, demote_measured=demote_measured
        )
    except ActivityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CanonicalTssConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RpeResponse(
        activity_id=activity_id,
        tss=tss,
        method_code=RPE_METHOD_CODE,
        needs_manual_rpe=False,
    )
