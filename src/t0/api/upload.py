"""HTTP endpoint for manual FIT/TCX/ZIP upload (L7 batch).

Currently LOCAL access only (documented product decision, see batch end report): no
Ingress, TLS, or authentication in place here. This module makes NO assumptions about
format from the filename extension announced by the HTTP client or from any URL: all
sniffing is delegated to :mod:`t0.api.ingest` / :mod:`t0.fit.archive`,
which only look at content.

Error -> HTTP status mapping, deliberately explicit (no generic "500" for expected
cases):

- :class:`~t0.api.errors.PayloadTooLargeError` -> 413 (request body too large).
- :class:`~t0.api.errors.HostileArchiveError` -> 422 (ZIP archive with dangerous
  pattern: path escape or decompression bomb).
- :class:`~t0.fit.errors.ArchiveError` -> 422 (content not recognized as FIT or
  TCX, ZIP archive empty/multi-member/invalid).
- Parsing failed after successful archive -> 422 also, but response body explicitly
  states that raw file was archived (no loss), to never suggest total failure when only
  ingestion failed.
- :class:`~t0.garmin.raw_store.FitFileShaConflictError` -> 409 (conflict):
  uploaded content already associated in database to a DIFFERENT source (real Garmin
  activity, or distinct manual upload). File stays archived on disk — see exception
  docstring for scenario details.
- Any other exception: allowed to propagate (FastAPI default 500) — a truly unexpected
  incident must not be masked as a "clean" response.

``python-multipart`` is required at runtime by FastAPI's ``File(...)``: absent from
this repository's venv at batch write time (see batch end report). This module is still
written and importable normally — only the actual route call with real ``multipart/
form-data`` body needs it.
"""

from __future__ import annotations

# EVERY name appearing in a route or dependency annotation below must be imported at
# runtime, never under TYPE_CHECKING. FastAPI resolves annotations through
# get_type_hints(); when that raises NameError it does NOT fail loudly -- it falls back
# to treating the parameter as an untyped query parameter, so every request answers 422
# "Field required" for a parameter that was meant to be injected. That is exactly what
# `DBConnection` under TYPE_CHECKING caused. Guarded by
# test_route_annotations_resolve_at_runtime.
from collections.abc import Iterator  # noqa: TC003
from pathlib import Path  # noqa: TC003
from typing import Annotated, cast

import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from t0.api.errors import HostileArchiveError, PayloadTooLargeError, UploadError
from t0.api.ingest import DEFAULT_MAX_UPLOAD_BYTES, ingest_upload
from t0.fit.errors import ArchiveError
from t0.garmin.raw_store import DBConnection, FitFileShaConflictError
from t0.settings import get_settings

router = APIRouter(prefix="/uploads", tags=["uploads"])

#: Size of one read chunk during request body streaming. Chosen small (1 MiB) so the
#: size bound (see :mod:`t0.api.ingest`) cuts excessive uploads quickly, without
#: materializing the entire body in memory first.
_READ_CHUNK_BYTES = 1024 * 1024


def get_upload_dest_dir() -> Path:
    """Default FastAPI dependency: archive directory read from settings.

    Overridable in tests via ``app.dependency_overrides`` (see ``tests/api/``), to never
    depend on Garmin/PostgreSQL environment variables in a test that only tests upload
    archiving.
    """
    return get_settings().fit_archive_path


def get_upload_db_conn() -> Iterator[DBConnection]:
    """Default FastAPI dependency: real PostgreSQL connection, one per request.

    Generator rather than simple return, so FastAPI closes the connection after the
    request regardless of outcome (success, 4xx, unexpected exception) — explicit
    ``finally`` rather than relying on garbage collector, which would leave PostgreSQL
    connections open under upload load.

    Overridable in tests via ``app.dependency_overrides`` (see ``tests/api/``), to inject
    in-memory stub without ever connecting to real PostgreSQL — necessary here since no
    PostgreSQL instance exists in this development environment (see batch end report).

    ``cast`` rather than ``psycopg.Connection`` returned as-is: same convention as
    ``t0.transform.__main__`` / ``t0.ops.__main__`` — mypy refuses to
    equate psycopg's OVERLOADED ``cursor`` method with a structural protocol that isn't.
    """
    conn = psycopg.connect(get_settings().postgres_dsn.get_secret_value())
    try:
        yield cast("DBConnection", conn)
    finally:
        conn.close()


class ParsedSummaryOut(BaseModel):
    """HTTP reflection of :class:`t0.api.ingest.ParsedSummary`."""

    n_samples: int
    n_laps: int
    n_swim_lengths: int
    has_session: bool
    start_time: str | None


class UploadResponse(BaseModel):
    """Response body of a processed upload, ingestion success or not.

    ``status`` is ``"ok"`` when both archiving and parsing succeeded, ``"archived_but_unparsed"``
    when only archiving succeeded (raw file is NOT lost, but ingestion cannot continue
    without intervention). Never ``"ok"`` without ``parsed`` being set: this field exists
    precisely so a caller cannot confuse the two cases by reading only the HTTP status.
    """

    status: str
    source_format: str
    sha256: str
    size_bytes: int
    parsed: ParsedSummaryOut | None
    parse_error: str | None
    fit_file_id: int


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read ``file`` in chunks, stopping as soon as ``max_bytes`` is exceeded.

    Avoids materializing in memory a request body already known to be excessive:
    unlike simple ``await file.read()`` followed by size check afterward, reading stops
    at the first chunk that crosses the bound.

    Raises:
        t0.api.errors.PayloadTooLargeError: cumulative size exceeds ``max_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"Upload rejected: at least {total} bytes received, above bound " f"({max_bytes})."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=UploadResponse, status_code=201)
async def upload_activity_file(
    file: Annotated[UploadFile, File(description=".fit, .tcx, or .zip archive file.")],
    dest_dir: Annotated[Path, Depends(get_upload_dest_dir)],
    conn: Annotated[DBConnection, Depends(get_upload_db_conn)],
) -> UploadResponse:
    """Receive a FIT/TCX/ZIP file, archive it first, insert its ``raw.fit_file`` row
    (``garmin_activity_id=None``), then attempt parsing."""
    # `_read_capped` is INSIDE the try: it is the first of the two places that raise
    # PayloadTooLargeError, and the earlier one -- it stops at the chunk that crosses the
    # bound, before the body is ever materialized. Left outside, an oversized upload
    # escaped as an unhandled exception and answered 500 instead of the 413 this module
    # documents.
    try:
        data = await _read_capped(file, DEFAULT_MAX_UPLOAD_BYTES)
        outcome = ingest_upload(
            data, dest_dir, max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES, conn=conn
        )
    except PayloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except HostileArchiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArchiveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FitFileShaConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UploadError as exc:  # pragma: no cover - safeguard if a subclass is added
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if outcome.fit_file_id is None:  # pragma: no cover - safeguard, `conn` is provided above
        raise RuntimeError(
            "ingest_upload returned fit_file_id=None even though a connection was "
            "provided: internal inconsistency between this route and t0.api.ingest."
        )

    parsed_out = (
        ParsedSummaryOut(
            n_samples=outcome.parsed.n_samples,
            n_laps=outcome.parsed.n_laps,
            n_swim_lengths=outcome.parsed.n_swim_lengths,
            has_session=outcome.parsed.has_session,
            start_time=(
                outcome.parsed.start_time.isoformat() if outcome.parsed.start_time else None
            ),
        )
        if outcome.parsed is not None
        else None
    )
    response = UploadResponse(
        status="ok" if outcome.parse_error is None else "archived_but_unparsed",
        source_format=outcome.archived.source_format,
        sha256=outcome.archived.sha256.hex(),
        size_bytes=outcome.archived.size_bytes,
        parsed=parsed_out,
        parse_error=outcome.parse_error,
        fit_file_id=outcome.fit_file_id,
    )
    if outcome.parse_error is not None:
        # Raw file IS archived (no loss) but ingestion failed: this is not success, so no
        # silent 201. Body above is still transmitted via `detail` so caller knows the
        # original is safe.
        raise HTTPException(status_code=422, detail=response.model_dump())
    return response
