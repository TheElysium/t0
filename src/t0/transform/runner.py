"""Orchestration of ``raw.fit_file`` -> ``core.*`` pass: the parsing queue.

This module is the missing link for the project to produce anything visible. Until now
the chain stopped at ``raw``: files were downloaded, archived on PVC, and logged in
``raw.fit_file``, but NOTHING wrote to ``core.*``. Exact consequence, not obvious: the
``mart.*`` views refreshed successfully on each ``ops refresh`` pass without error, yet
stayed empty -- Grafana dashboards showed nothing with no red light.

The queue is not invented here: ``raw.fit_file`` carries it since migration 002, with
``parsed_at`` / ``parser_version`` / ``parse_error`` and partial index
``fit_file_unparsed_idx ON raw.fit_file (downloaded_at) WHERE parsed_at IS NULL``.
This module just consumes it in the order that index serves.

WRITE ORDER, AND WHY THAT ONE. The bundle is written and VALIDATED first, then
``parsed_at`` is set in a second transaction. This is not an oversight: it is the same
reasoning as the atomicity rule "file on PVC BEFORE its row in database" from the
``raw.fit_file`` comment, applied one level deeper. A crash between the two leaves an
activity written in ``core`` and a file still marked unparsed -- the next pass retakes
it, and since :func:`~t0.transform.store.store_activity_bundle` is idempotent
(full replacement per activity), the final state is the same. The reverse order would
produce a file marked parsed whose activity was never written: a session lost silently,
never retaken by any pass.

WHY A FAILURE IS NOT RETRIED AUTOMATICALLY. The default selection adds ``AND parse_error
IS NULL`` to the ``parsed_at IS NULL`` condition. Without it, a permanently indigestible
file -- a Garmin ``typeKey`` missing from the lookup table, a truncated archive -- would
be retried on every nightly pass, fail identically, forever. ``retry_failed=True``
(``--retry-failed`` option) explicitly retries these rows, which is exactly what we want
AFTER fixing the cause: it is an operator decision, not default behavior.

MISSING SUMMARY ≠ FAILURE. ``build_core_rows`` needs an ``ActivityContext`` only the
Garmin JSON summary carries (sport, timezone, duration). If that summary is not yet in
``raw.api_response``, the file is IGNORED without being marked in error: it is a normal
transient state (the .FIT file and today's ``get_activities_by_date`` window are not
necessarily ingested by the same pass), and marking it in error would exclude it from
the default selection until manual intervention, for a cause that resolves itself on the
next poll.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from t0.fit.reader import decode_fit_file
from t0.tcx.reader import decode_tcx_file
from t0.transform.activity import build_core_rows
from t0.transform.manual import build_manual_activity_context
from t0.transform.store import store_activity_bundle
from t0.transform.summary import build_activity_context, load_thresholds, select_thresholds

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from t0.load.thresholds import Threshold
    from t0.load.trimp import Sex

logger = logging.getLogger(__name__)

#: Written to ``raw.fit_file.parser_version`` on each success. Its purpose is written
#: in the column comment: "lets us selectively replay files parsed by a buggy version".
#: It only helps if it CHANGES when decode or transform behavior changes -- increment
#: deliberately, never auto-derived from package version.
PARSER_VERSION = "t0-transform/1"

#: Limit per pass. A nightly CronJob has no reason to process all 1,090 historical
#: files on first start: better several short passes, each leaving the database in
#: consistent state, than one multi-hour pass a pod eviction would reset to zero.
DEFAULT_BATCH_LIMIT = 200

_PENDING_QUERY_NOT_FAILED = (
    "SELECT fit_file_id, garmin_activity_id, storage_path "
    "FROM raw.fit_file "
    "WHERE parsed_at IS NULL AND parse_error IS NULL "
    "ORDER BY downloaded_at "
    "LIMIT %s"
)

_PENDING_QUERY_ALL = (
    "SELECT fit_file_id, garmin_activity_id, storage_path "
    "FROM raw.fit_file "
    "WHERE parsed_at IS NULL "
    "ORDER BY downloaded_at "
    "LIMIT %s"
)


class _Cursor(Protocol):
    """Minimal surface required of a cursor by THIS module.

    ``executemany`` appears here even though no function here calls it:
    :func:`~t0.transform.store.store_activity_bundle`, to which the connection
    is passed as-is, needs it. Without it, this protocol would not be a subtype of
    ``store``'s, and the handoff would not type-check -- yet it would work at runtime.
    Better to state it than work around it with a ``cast``.
    """

    def execute(self, query: str, params: Any = ...) -> object: ...
    def executemany(self, query: str, params_seq: Sequence[Any]) -> object: ...
    def fetchall(self) -> list[Any]: ...
    def fetchone(self) -> Any: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Structural view of a psycopg 3 connection, same pattern as
    ``t0.garmin.raw_store``, extended with ``rollback``: this module needs it
    in a way its neighbors do not, explained in :func:`_mark_failed`."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingFile:
    """A row from ``raw.fit_file`` awaiting transformation."""

    fit_file_id: int
    #: NULL for manual upload: migration 012 made the column nullable, NULL being the
    #: only provenance marker (Garmin API never produces a row without activityId).
    garmin_activity_id: int | None
    storage_path: str


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """Outcome of a file. ``status`` is one of ``stored`` | ``skipped`` | ``failed``."""

    fit_file_id: int
    garmin_activity_id: int | None
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TransformReport:
    """Summary of a pass, for CronJob stdout."""

    outcomes: tuple[FileOutcome, ...]
    #: Files carrying a ``parse_error`` anywhere in the table, not just this pass. A
    #: failed file exits the default selection: without this total, the next pass finds
    #: it absent from the queue, exits at 0, and the activity is permanently missing from
    #: ``core`` with no red light.
    failed_backlog: int = 0

    @property
    def stored(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "stored")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")


def count_failed_files(conn: DBConnection) -> int:
    """Count of files carrying a ``parse_error``, across all passes.

    A file marked failed exits the default selection: it is never retried without
    ``--retry-failed``. Counting it each pass is what prevents a single failure from
    becoming a permanent hole in ``core`` with no one noticing.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.fit_file WHERE parse_error IS NOT NULL")
        row = cur.fetchone()
    return 0 if row is None else int(row[0])


def select_pending_files(
    conn: DBConnection, *, limit: int = DEFAULT_BATCH_LIMIT, retry_failed: bool = False
) -> tuple[PendingFile, ...]:
    """Files awaiting transformation, oldest download to newest.

    ``ORDER BY downloaded_at`` is not cosmetic: it is the partial index
    ``fit_file_unparsed_idx`` column, so the only order PostgreSQL can serve without
    sort. Processing oldest-to-newest also has a business consequence: PMC is built
    chronologically, and a pass interrupted mid-way leaves a continuous history to a
    date, rather than gaps.
    """
    # Two queries WRITTEN WHOLE rather than interpolated fragment: nothing here comes
    # from outside, but a query built by f-string eventually becomes the template
    # copied elsewhere with values that do come from outside.
    query = _PENDING_QUERY_ALL if retry_failed else _PENDING_QUERY_NOT_FAILED
    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        rows = cur.fetchall()
    return tuple(
        PendingFile(
            fit_file_id=int(r[0]),
            # `int(None)` would raise here, OUTSIDE file isolation: a single manual
            # upload would fail the entire nightly pass.
            garmin_activity_id=None if r[1] is None else int(r[1]),
            storage_path=str(r[2]),
        )
        for r in rows
    )


def load_activity_summary(conn: DBConnection, activity_id: int) -> Mapping[str, Any] | None:
    """JSON summary of ONE activity, extracted from the latest ``get_activities_by_date``
    payload.

    Garmin does not serve summaries one-by-one: ``get_activities_by_date`` returns an
    ARRAY of activities for a date window, stored as-is in ``raw.api_response``.
    Finding one specific activity requires unfolding that array (``jsonb_array_elements``)
    rather than filtering on ``scope_key`` -- which stays NULL for this endpoint, lacking
    a unique key to characterize it. The cost is scanning endpoint rows; it is accepted
    here, one query per pending file.

    ``ORDER BY fetched_at DESC LIMIT 1`` takes the MOST RECENT version: the table is
    append-only and Garmin republishes data (retroactive recalculations). Taking the
    oldest would freeze the summary at first-known state, exactly against this table's
    purpose.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT elem "
            "FROM raw.api_response r, jsonb_array_elements(r.payload) AS elem "
            "WHERE r.endpoint = 'get_activities_by_date' "
            "  AND jsonb_typeof(r.payload) = 'array' "
            "  AND elem->>'activityId' = %s "
            "ORDER BY r.fetched_at DESC "
            "LIMIT 1",
            (str(activity_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = row[0]
    # psycopg already decodes jsonb to a Python object; the str branch covers a test
    # double or differently-configured driver, without deciding which.
    if isinstance(payload, str):
        decoded: Any = json.loads(payload)
        return decoded if isinstance(decoded, dict) else None
    return payload if isinstance(payload, dict) else None


def resolve_athlete_id(conn: DBConnection) -> int:
    """Unique ``athlete_id`` from ``core.athlete``.

    Raises if the table is empty or has more than one row, instead of defaulting to 1.
    Attaching an entire history to the wrong athlete would produce wrong thresholds,
    thus wrong TSS, undetected by any constraint -- the error only shows up reading
    curves, much later.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT athlete_id FROM core.athlete ORDER BY athlete_id")
        rows = cur.fetchall()
    if len(rows) != 1:
        msg = (
            f"core.athlete has {len(rows)} row(s): cannot choose alone. "
            "Pass --athlete-id explicitly."
        )
        raise ValueError(msg)
    return int(rows[0][0])


def load_athlete_sex(conn: DBConnection, athlete_id: int) -> Sex:
    """Athlete's ``sex``, required by Banister TRIMP.

    This is not civil status but a calculation coefficient: k is 0.64/1.92 for male,
    0.86/1.67 for female. Getting it wrong underestimates or overestimates TRIMP by ~15%
    across all load, uniformly and invisibly -- exactly the kind of error nothing catches.

    Raises if athlete is unknown: mistyped ``--athlete-id`` must fail here, not attach
    history to a nonexistent row.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT sex FROM core.athlete WHERE athlete_id = %s", (athlete_id,))
        row = cur.fetchone()
    if row is None:
        msg = f"core.athlete has no athlete_id = {athlete_id}."
        raise ValueError(msg)
    value = str(row[0])
    if value not in ("male", "female"):
        # Migration 003's CHECK constraint forbids it; if we get here, the schema has
        # diverged, and guessing is worse than stopping.
        msg = f"core.athlete.sex = {value!r}: value not in ('male','female')."
        raise ValueError(msg)
    return cast("Sex", value)


def load_home_timezone(conn: DBConnection, athlete_id: int) -> str:
    """``core.athlete.home_timezone``, read ONCE per pass (same reasoning as thresholds:
    an invariant value re-read per file would be hundreds of identical queries).

    Sanctioned by the column's own comment (migrations/003_core_ref.sql) as the fallback
    "when an activity's timezone is indeterminable" -- exactly the manual-upload case,
    which carries no ``startTimeGMT``/``startTimeLocal`` pair to derive a real offset from.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT home_timezone FROM core.athlete WHERE athlete_id = %s", (athlete_id,))
        row = cur.fetchone()
    if row is None:
        msg = f"core.athlete has no athlete_id = {athlete_id}."
        raise ValueError(msg)
    return str(row[0])


def _resolve_manual_activity_id(conn: DBConnection, fit_file_id: int) -> int | None:
    """Idempotence (migrations/014): a manual activity already written for this file is
    REUSED rather than re-drawn from the sequence, so a second transformation pass never
    creates a duplicate -- the partial UNIQUE index ``activity_manual_file_uq`` would
    reject it anyway, but failing on retry would be worse than simply finding it again.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT activity_id FROM core.activity WHERE fit_file_id = %s AND source = 'manual'",
            (fit_file_id,),
        )
        row = cur.fetchone()
    return None if row is None else int(row[0])


def _next_manual_activity_id(conn: DBConnection) -> int:
    """Fresh identifier from ``core.manual_activity_id_seq`` (migrations/014) for a
    manual upload never transformed before."""
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('core.manual_activity_id_seq')")
        row = cur.fetchone()
    if row is None:
        msg = "core.manual_activity_id_seq: nextval() returned no row."
        raise RuntimeError(msg)
    return int(row[0])


def decode_archived_file(path: Path, *, activity_id: int) -> Any:
    """Choose decoder by the extension set at archive time.

    This extension is not a guess about the name Garmin sent: ``t0.fit.archive``
    sniffs the actual archive member content and DERIVES the suffix from it. Trusting it
    here means trusting an observed fact.
    """
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return decode_fit_file(path, activity_id=activity_id)
    if suffix == ".tcx":
        return decode_tcx_file(path, activity_id=activity_id)
    msg = f"unexpected extension {suffix!r} for {path}: neither .fit nor .tcx."
    raise ValueError(msg)


def _mark_parsed(conn: DBConnection, fit_file_id: int) -> None:
    """Set ``parsed_at`` / ``parser_version`` and CLEAR ``parse_error``.

    Clearing matters: a file that failed then succeeded must not keep its old failure
    message, or a health check would forever flag it as problematic when it is in the
    database and correct.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.fit_file "
            "SET parsed_at = now(), parser_version = %s, parse_error = NULL "
            "WHERE fit_file_id = %s",
            (PARSER_VERSION, fit_file_id),
        )
    conn.commit()


def _mark_failed(conn: DBConnection, fit_file_id: int, message: str) -> None:
    """Record failure, leaving ``parsed_at`` as NULL.

    The prior ``rollback()`` is essential, and why this module requires ``rollback`` in
    its protocol unlike its neighbors: after SQL error, psycopg sets transaction to
    "aborted" and refuses any further query. Without prior rollback, writing the error
    message itself would fail -- and the incident vanishes with no trace, exactly opposite
    what we want here.

    The message is truncated: ``parse_error`` is a limitless ``text``, but several
    kilobytes per file would make the column unreadable when reviewing (diagnostic query
    in ``docs/RUNBOOK.md``).
    """
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw.fit_file SET parse_error = %s WHERE fit_file_id = %s",
            (message[:2000], fit_file_id),
        )
    conn.commit()


def transform_one(
    conn: DBConnection,
    pending: PendingFile,
    *,
    athlete_id: int,
    sex: Sex,
    thresholds: Sequence[Threshold],
    home_timezone: str,
) -> FileOutcome:
    """Transform ONE file. Never lets errors for this file escape.

    An indigestible file must not crash the entire pass: other sessions have nothing to
    do with it. Error is captured, written to ``raw.fit_file.parse_error``, and the pass
    continues. The column is read with the diagnostic query from ``docs/RUNBOOK.md``;
    ``format_transform_report`` shows the total each pass.

    Capture covers summary reading, manual-upload identifier resolution, and
    ``parsed_at`` setting, not just the computation: a truncated ``raw.api_response``
    payload, an unrecognized manual-upload sport, or an issue on the final ``UPDATE``
    would otherwise bubble to the caller, killing the pass without printing any report --
    the operator would not know where it stopped.
    """
    activity_id = pending.garmin_activity_id

    try:
        if activity_id is None:
            # Manual upload (migration 012 made garmin_activity_id nullable; migration
            # 014 gave it a key). No Garmin summary exists for this file: sport,
            # duration, and timezone are all derived from the archived file itself by
            # `build_manual_activity_context`, never from `raw.api_response`.
            activity_id = _resolve_manual_activity_id(conn, pending.fit_file_id)
            if activity_id is None:
                activity_id = _next_manual_activity_id(conn)
            decoded = decode_archived_file(Path(pending.storage_path), activity_id=activity_id)
            context = build_manual_activity_context(
                decoded,
                activity_id=activity_id,
                home_timezone=home_timezone,
                tcx_sport=decoded.tcx_sport,
            )
        else:
            summary = load_activity_summary(conn, activity_id)
            if summary is None:
                # Transient state, NOT an error: see module docstring.
                logger.info(
                    "activity %s: get_activities_by_date summary missing, file %s deferred.",
                    activity_id,
                    pending.fit_file_id,
                )
                # The read above opened a transaction this path never otherwise closes: a
                # batch entirely deferred would leave the session `idle in transaction`
                # and block VACUUM progress on raw.*.
                conn.rollback()
                return FileOutcome(
                    pending.fit_file_id, activity_id, "skipped", "Garmin summary missing"
                )

            context = build_activity_context(summary)
            decoded = decode_archived_file(Path(pending.storage_path), activity_id=activity_id)

        threshold, all_threshold = select_thresholds(
            thresholds, context.discipline, context.start_time_local.date()
        )
        bundle = build_core_rows(
            decoded,
            context,
            athlete_id=athlete_id,
            fit_file_id=pending.fit_file_id,
            threshold=threshold,
            all_threshold=all_threshold,
            sex=sex,
        )
        store_activity_bundle(conn, bundle)
        # After bundle commit, never before: see "write order" at module start.
        _mark_parsed(conn, pending.fit_file_id)
    except Exception as exc:  # isolate this file's error, cf. docstring.
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("activity %s: transform failed -- %s", activity_id, message)
        _mark_failed(conn, pending.fit_file_id, message)
        return FileOutcome(pending.fit_file_id, activity_id, "failed", message)

    logger.info("activity %s written to core (%s samples).", activity_id, len(bundle.samples))
    return FileOutcome(pending.fit_file_id, activity_id, "stored")


def transform_pending_files(
    conn: DBConnection,
    *,
    athlete_id: int | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    retry_failed: bool = False,
) -> TransformReport:
    """Process a batch of pending files and return the pass summary."""
    resolved_athlete = athlete_id if athlete_id is not None else resolve_athlete_id(conn)
    sex = load_athlete_sex(conn, resolved_athlete)
    # Thresholds are loaded ONCE for the entire batch: they are historized by validity
    # period, not per activity, and re-reading per file would be hundreds of identical
    # queries for an invariant result.
    thresholds = load_thresholds(conn, athlete_id=resolved_athlete)
    # Same reasoning for home_timezone: an invariant fallback value, read once rather
    # than once per manual-upload file.
    home_timezone = load_home_timezone(conn, resolved_athlete)
    pending = select_pending_files(conn, limit=limit, retry_failed=retry_failed)
    logger.info("%s file(s) awaiting transformation.", len(pending))
    outcomes = tuple(
        transform_one(
            conn,
            item,
            athlete_id=resolved_athlete,
            sex=sex,
            thresholds=thresholds,
            home_timezone=home_timezone,
        )
        for item in pending
    )
    return TransformReport(outcomes=outcomes, failed_backlog=count_failed_files(conn))


def format_transform_report(report: TransformReport) -> str:
    """Readable report to stdout, in line with other entry points.

    Written activities are not detailed line-by-line: on a 200-file catchup, only
    abnormal cases deserve reading.
    """
    lines = [
        f"Transform: {report.stored} stored, {report.skipped} deferred, " f"{report.failed} failed."
    ]
    lines.extend(
        f"  activity {o.garmin_activity_id} -- {o.status}: {o.detail}"
        for o in report.outcomes
        if o.status != "stored"
    )
    if report.failed_backlog:
        lines.append(
            f"WARNING: {report.failed_backlog} file(s) failed outside queue "
            "(see docs/RUNBOOK.md section 10, then --retry-failed)."
        )
    return "\n".join(lines)
