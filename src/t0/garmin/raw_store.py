"""Write to ``raw.api_response`` and ``raw.ingest_run`` (migration ``002_raw.sql``).

ELT principle "raw first" (research 05 §1): this module performs **no** transformation
of the payload — it serializes it canonically (sorted keys) for integrity hash, and
writes it as-is. All parsing/typing logic lives in downstream batches (L3+), which
replay ``raw`` without ever re-requesting Garmin.

DB boundary chosen for this batch: the functions below accept a connection typed by the
structural protocol :class:`DBConnection` (a simple "cursor + commit" view on the
psycopg 3 API), not ``psycopg.Connection`` directly. Two reasons:

1. :mod:`t0.db` (batch L1) exposes no generic query utilities — only the
   migrations runner — so nothing to reuse from there.
2. Typing against a minimal structural protocol lets tests inject a fake in-memory
   connection (no PostgreSQL required) while still verified by mypy, since
   ``psycopg.Connection`` satisfies this protocol structurally.

This module opens no connection itself: the caller (the service entry point, out of scope
for this batch) must construct the real connection via
``psycopg.connect(settings.postgres_dsn.get_secret_value())`` and pass it here.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from datetime import date


class _Cursor(Protocol):
    def execute(self, query: str, params: Any = ...) -> object: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def __enter__(self) -> _Cursor: ...
    def __exit__(self, *exc_info: object) -> None: ...


class DBConnection(Protocol):
    """Minimal structural view of a psycopg 3 connection for test injection."""

    def cursor(self) -> _Cursor: ...
    def commit(self) -> None: ...


def canonical_json_dumps(payload: object) -> str:
    """Canonical serialization: sorted keys, fixed separators — basis of integrity hash."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def payload_sha256(payload: object) -> bytes:
    """sha256 (32 raw bytes) of canonically serialized payload.

    Matches ``raw.api_response.payload_sha256`` (``CHECK length = 32``).
    """
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).digest()


def insert_api_response(
    conn: DBConnection,
    *,
    endpoint: str,
    payload: object,
    http_status: int = 200,
    scope_date: date | None = None,
    scope_key: str | None = None,
    ingest_run_id: int | None = None,
) -> int:
    """Insert a row into ``raw.api_response`` and return its ``id``.

    ``http_status`` defaults to 200: ``garminconnect`` 0.3.2 does not expose the real
    HTTP code for a successful call (it deserializes JSON directly), so this module can
    only assert the logical success of the call — a deliberate choice, documented in
    the batch report rather than hidden behind a made-up value without comment.
    """
    sha = payload_sha256(payload)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.api_response
                (endpoint, scope_date, scope_key, http_status, payload, payload_sha256,
                 ingest_run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                endpoint,
                scope_date,
                scope_key,
                http_status,
                Jsonb(payload),
                sha,
                ingest_run_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError("INSERT raw.api_response returned no row (RETURNING id).")
    return int(row[0])


class FitFileShaConflictError(RuntimeError):
    """``sha256`` already associated in DB with a DIFFERENT activity than requested.

    "Different" also compares origin: ``None`` (manual upload, migration
    ``012_raw_fit_file_manual_upload.sql``) is never confused with a real
    ``garmin_activity_id``. Identical binary content reused between a manual upload and
    an authentic Garmin activity — or between two distinct Garmin activities — is
    exactly the kind of collision this exception makes visible rather than silently
    attributing the row to the wrong origin.

    Real scenario identified in review (batch L12, this athlete is triathlon): a
    brick/multisport session produces a parent activity and child activities (run, bike,
    swim), each with its own ``garmin_activity_id`` — and the 'original' download of
    several of these ids can serve EXACTLY the same session file (identical bytes, same
    sha256). Another plausible case: an identical error placeholder served by Garmin for
    two unavailable activities.

    Without this detection, :func:`insert_fit_file` would silently return the
    ``fit_file_id`` of the OTHER activity (the one that won the insertion race): no row
    would reference the activity requested here, its freshly-archived file would become
    a permanent orphan, and a caller building on it in good faith would point a
    ``fit_file_id`` at the wrong activity (an L3 setting ``parsed_at`` would mark a row
    for an activity other than the one actually parsed). This exception makes the case
    visible rather than lying about status.

    Accepted limit, documented rather than hidden: the real fix would be to allow
    multiple ``raw.fit_file`` rows for one ``sha256`` (one per referencing activity),
    which requires dropping ``fit_file_sha_uq`` — a change to frozen DDL
    (``migrations/002_raw.sql``), out of scope for this batch. The minimum required
    here is never claiming insertion succeeded when it was actually refused for the
    wrong reason.
    """


def insert_fit_file(
    conn: DBConnection,
    *,
    garmin_activity_id: int | None,
    storage_path: str,
    sha256: bytes,
    size_bytes: int,
) -> int:
    """Insert a row into ``raw.fit_file`` and return its ``fit_file_id``.

    Called by :mod:`t0.scheduler.activity_files` (batch L12) **after**, never
    before, the file has been written and verified on PVC by
    :func:`t0.fit.archive.archive_download` — this function never touches the
    volume, only records an already-established disk fact. Also called by
    :func:`t0.api.ingest.ingest_upload` (batch L7) for manual upload with
    ``garmin_activity_id=None`` — see ``migrations/012_raw_fit_file_manual_upload.sql``,
    which made the column nullable precisely for this second caller and set the
    convention: ``None`` here means "manual upload", never anything else.

    Conflict management on ``sha256`` (``fit_file_sha_uq``, ``UNIQUE``): identical
    binary content re-downloaded for the SAME activity (common case: ``run_scheduled_poll``
    in batch L6 re-probes the current day, so this module is called multiple times on
    recent activities) means "already archived" — conflict re-read then returns the
    existing ``id``, without duplicating or overwriting a row. Choice remains ``DO
    NOTHING`` rather than ``DO UPDATE``: an existing ``raw.fit_file`` row must **never**
    be silently rewritten (it may already carry ``parsed_at``/``parser_version`` set by
    L3 — naive ``DO UPDATE`` on only this module's columns would overwrite them with
    ``NULL``). Identical manual upload returned twice (same sha256, both with
    ``garmin_activity_id=None``) follows the exact same path: ``None == None`` is an
    origin agreement, not a divergence.

    Identical binary content attached to a DIFFERENT activity (different
    ``garmin_activity_id``, including ``None``) is no longer treated as the same case —
    see :class:`FitFileShaConflictError` for the multisport scenario that motivated this
    distinction, identified in review, now extended to the Garmin/manual-upload
    distinction. Conflict re-read now compares the activity (or its absence), not just
    the sha256.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.fit_file
                (garmin_activity_id, storage_path, sha256, size_bytes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (sha256) DO NOTHING
            RETURNING fit_file_id;
            """,
            (garmin_activity_id, storage_path, sha256, size_bytes),
        )
        inserted_row = cur.fetchone()
        conflict_row: tuple[Any, ...] | None = None
        if inserted_row is None:
            # Conflict on sha256: row already exists -- but potentially for a DIFFERENT
            # activity (brick/multisport, see FitFileShaConflictError), so re-read must
            # also check garmin_activity_id, not just fit_file_id as before review.
            cur.execute(
                "SELECT fit_file_id, garmin_activity_id FROM raw.fit_file WHERE sha256 = %s;",
                (sha256,),
            )
            conflict_row = cur.fetchone()
    conn.commit()

    if inserted_row is not None:
        return int(inserted_row[0])

    if conflict_row is None:
        raise RuntimeError(
            "INSERT raw.fit_file conflicted on sha256 but no existing row found on "
            "re-read: unexpected inconsistency between the two queries."
        )
    existing_fit_file_id = int(conflict_row[0])
    # existing_activity_id is NOT forced to int: the re-read row may itself be a manual
    # upload (NULL in DB). int(None) would raise exactly the flaw this batch corrects
    # in transform.runner -- do not reproduce it here.
    existing_activity_id: int | None = None if conflict_row[1] is None else int(conflict_row[1])
    if existing_activity_id != garmin_activity_id:
        existing_label = (
            "manual upload" if existing_activity_id is None else str(existing_activity_id)
        )
        requested_label = "manual upload" if garmin_activity_id is None else str(garmin_activity_id)
        raise FitFileShaConflictError(
            f"sha256 already associated with activity {existing_label} "
            f"(fit_file_id={existing_fit_file_id}); insertion refused for activity "
            f"{requested_label} -- identical content served for two different origins "
            "(multisport/brick session sharing one session file, identical error "
            "placeholder, or manual upload of a file already archived from Garmin). "
            "No row was created for this request."
        )
    return existing_fit_file_id


def start_ingest_run(
    conn: DBConnection,
    *,
    mode: str,
    code_version: str | None = None,
) -> int:
    """Open a ``raw.ingest_run`` row (status ``running``) and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.ingest_run (mode, status, code_version)
            VALUES (%s, 'running', %s)
            RETURNING ingest_run_id;
            """,
            (mode, code_version),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError("INSERT raw.ingest_run returned no row (RETURNING ingest_run_id).")
    return int(row[0])


def finish_ingest_run(
    conn: DBConnection,
    *,
    ingest_run_id: int,
    status: str,
    notes: dict[str, Any] | None = None,
) -> None:
    """Close a ``raw.ingest_run`` row: ``finished_at``, ``status``, ``notes``.

    ``status`` must be one of the values allowed by the ``ingest_run_status_ck``
    constraint (``ok``, ``partial``, ``failed``) — ``running`` is never a valid closing
    status, this module does not validate it itself: PostgreSQL refuses it via the
    ``CHECK`` constraint, by design (single source of truth for permitted values).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE raw.ingest_run
               SET finished_at = now(), status = %s, notes = %s
             WHERE ingest_run_id = %s;
            """,
            (status, Jsonb(notes) if notes is not None else None, ingest_run_id),
        )
    conn.commit()
