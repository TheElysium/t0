"""Business logic for manual upload ingestion (L7 batch), independent of any HTTP
framework.

This module knows nothing of FastAPI: it takes bytes already read into memory and a
destination directory, and returns a structured result or raises an error from
:mod:`t0.api.errors` / :mod:`t0.fit.errors`. It is ``t0.api.upload``
(the HTTP layer) that maps these errors to status codes. This separation allows testing
all business logic without depending on ``python-multipart`` or ``httpx`` (see batch
end report: neither is installed in the repository venv).

Sequence mandated by the batch mission, in this order and never otherwise:

1. Size bound on received bytes (:class:`~t0.api.errors.PayloadTooLargeError`).
2. If content is a ZIP archive, pre-inspect member metadata (names, declared sizes)
   BEFORE any real decompression, to reject path escapes and decompression bombs
   (:class:`~t0.api.errors.HostileArchiveError`).
3. Archive-first storage via :func:`t0.fit.archive.archive_download`: file is
   written and verified to disk before any parsing attempt — this is the guarantee
   already offered by that module (L3), carried through as-is here.
4. Attempt parsing (:func:`t0.fit.reader.decode_fit` /
   :func:`t0.tcx.reader.decode_tcx`) to validate that archived content is
   actually usable, not just recognized by its signature.

5. If a caller provides a connection (``conn``), record the ``raw.fit_file`` row via
   :func:`t0.garmin.raw_store.insert_fit_file` with ``garmin_activity_id=None``
   — the convention set by ``migrations/012_raw_fit_file_manual_upload.sql`` to mark a
   manual upload. Done AFTER step 3 (never before): this is the central invariant of the
   ``raw.fit_file`` module, independent of row provenance.

``conn`` remains optional (see :func:`ingest_upload`): this batch does not require a
database to validate an upload (useful in tests, or for future offline use), but the
HTTP layer (:mod:`t0.api.upload`) always provides a real one — otherwise the
archived file would stay out of the transformation queue, exactly the problem migration
012 solved on the schema side. The ``activity_id`` passed to ``decode_fit``/``decode_tcx``
below is a sentinel identifier purely internal to parsing validation (step 4): it is
never persisted, never returned to the caller, and must never be confused with a real
Garmin activity ID nor with the ``garmin_activity_id=None`` inserted in step 5.

Scope note (see docs/RUNBOOK.md section 11): a manual upload never has an associated
Garmin summary (no ``activityId``, thus no matching ``raw.api_response`` row), but it IS
transformed all the way to ``core.*`` — migration 014 gave it a path of its own.
:mod:`t0.transform.runner` sees ``garmin_activity_id IS NULL``, draws an
identifier from ``core.manual_activity_id_seq`` (idempotent through the partial unique
index ``activity_manual_file_uq``) and hands the file to
:func:`t0.transform.manual.build_manual_activity_context`, which derives sport,
timezone and duration from the archived file itself. The "deferred" (``skipped``) status
concerns a DIFFERENT case: a file that HAS a ``garmin_activity_id`` whose summary has not
been ingested yet — never a manual upload.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from t0.api.errors import HostileArchiveError, PayloadTooLargeError
from t0.fit.archive import ArchivedFile, archive_download
from t0.fit.errors import SourceParseError
from t0.fit.reader import decode_fit
from t0.garmin.raw_store import insert_fit_file
from t0.tcx.reader import decode_tcx

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from t0.garmin.raw_store import DBConnection

#: Maximum accepted size for upload body (200 MiB). A real FIT or TCX activity file,
#: even a multi-hour ultramarathon, fits well below this: this bound protects service
#: memory against abnormal uploads, not legitimate use.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Maximum accepted size for DECOMPRESSED content of a ZIP archive member. Same bound
#: as above: legitimate FIT/TCX has no reason to exceed 200 MiB once decompressed.
DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES = DEFAULT_MAX_UPLOAD_BYTES

#: Ratio (decompressed size / compressed size) beyond which a member is treated as a
#: decompression bomb rather than legitimate file. TCX (XML, repetitive text) compresses
#: well but stays well below this threshold in practice; classic decompression bombs
#: (e.g. 42.zip) target ratios of many thousands, well above.
DEFAULT_MAX_COMPRESSION_RATIO = 200.0

#: ZIP signatures recognized (same as ``t0.fit.archive._ZIP_SIGNATURES``:
#: duplicated here rather than imported because that name is private to that module, and
#: this pre-inspection is specific to the HTTP layer, not archiving itself).
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")


def _looks_like_zip(data: bytes) -> bool:
    return any(data[: len(sig)] == sig for sig in _ZIP_SIGNATURES)


def _is_safe_member_name(name: str) -> bool:
    """Reject any ZIP member name bearing a path escape.

    Defense in depth: ``t0.fit.archive`` already never uses this name to write
    to disk (it reads content by index and picks its own final name), so path escape is
    not exploitable in current code. But a suspicious member name remains a strong signal
    of a hostile or malformed archive, to reject explicitly rather than pass silently.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    if ":" in name:  # Windows absolute path, e.g. "C:\\evil"
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def check_zip_safety(
    data: bytes,
    *,
    max_member_uncompressed_bytes: int = DEFAULT_MAX_MEMBER_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    """Pre-inspect ZIP archive metadata without decompressing anything.

    Only reads :meth:`zipfile.ZipFile.infolist` (central directory metadata, already
    read by normal ``ZipFile()`` init): no member content bytes are decompressed here,
    so a decompression bomb cannot consume memory during this check itself.

    A simply invalid archive (:class:`zipfile.BadZipFile`) is NOT a hostile archive:
    this is not where it should be reported. This function stays silent in that case and
    lets :func:`t0.fit.archive.archive_download`, called right after, raise the
    appropriate :class:`~t0.fit.errors.ArchiveError` (message "invalid ZIP
    archive").

    Raises:
        HostileArchiveError: member name bearing path escape, excessive declared
            decompressed size, or excessive compression ratio.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not _is_safe_member_name(info.filename):
                raise HostileArchiveError(
                    f"ZIP archive rejected: suspicious member name {info.filename!r} "
                    "(path escape)."
                )
            if info.file_size > max_member_uncompressed_bytes:
                raise HostileArchiveError(
                    f"ZIP archive rejected: member {info.filename!r} declares "
                    f"{info.file_size} decompressed bytes, above bound "
                    f"({max_member_uncompressed_bytes})."
                )
            compressed = max(info.compress_size, 1)
            ratio = info.file_size / compressed
            if ratio > max_compression_ratio:
                raise HostileArchiveError(
                    f"ZIP archive rejected: member {info.filename!r} has compression "
                    f"ratio {ratio:.0f} (bound {max_compression_ratio:.0f}), "
                    "characteristic signature of a decompression bomb."
                )


#: Internal sentinel identifier, never persisted (see module docstring).
_VALIDATION_ACTIVITY_ID = -1


@dataclass(frozen=True, slots=True)
class ParsedSummary:
    """Summary of parsing a stored file, for HTTP response.

    Never includes samples themselves (potentially tens of thousands): only counts and
    presence/absence of key information, enough to confirm the file is actually usable
    without bloating the HTTP response.
    """

    n_samples: int
    n_laps: int
    n_swim_lengths: int
    has_session: bool
    start_time: datetime | None


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """Complete result of processed upload: always archived if returned without raising.

    ``parsed`` is ``None`` exactly when ``parse_error`` is not ``None`` — the two are
    never both set, never both absent.

    ``fit_file_id`` is ``None`` exactly when :func:`ingest_upload` was called without
    ``conn`` (pure validation, no database): this is never a sign of insertion failure;
    failed insertion raises (``FitFileShaConflictError`` in particular) rather than
    silently returning a result without ID.
    """

    archived: ArchivedFile
    parsed: ParsedSummary | None
    parse_error: str | None
    fit_file_id: int | None = None


def _summarize(data: bytes, source_format: str) -> ParsedSummary:
    if source_format == "fit":
        parsed = decode_fit(data, activity_id=_VALIDATION_ACTIVITY_ID)
    elif source_format == "tcx":
        parsed = decode_tcx(data, activity_id=_VALIDATION_ACTIVITY_ID)
    else:  # pragma: no cover - archive_download never returns anything else
        raise SourceParseError(f"Unexpected source format after archiving: {source_format!r}.")
    return ParsedSummary(
        n_samples=len(parsed.samples),
        n_laps=len(parsed.laps),
        n_swim_lengths=len(parsed.swim_lengths),
        has_session=parsed.session is not None,
        start_time=parsed.start_time,
    )


def ingest_upload(
    data: bytes,
    dest_dir: Path,
    *,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    conn: DBConnection | None = None,
) -> UploadOutcome:
    """Single entry point for L7 batch: size bound, hostile-ZIP guard, archive-first
    storage, ``raw.fit_file`` insert (if ``conn`` provided), then parsing validation.

    Stored filename is derived from sha256 of UPLOADED content (before any ZIP
    extraction), never from the HTTP client's announced filename or archive member name:
    content-addressed, thus immune to path-escape attempts in naming, and naturally
    idempotent (same upload always produces same final name).

    ``conn`` remains optional: without it, this function only validates and archives
    (useful in unit tests, no PostgreSQL required); with it, the ``raw.fit_file`` row is
    inserted with ``garmin_activity_id=None`` (see module docstring) and the file truly
    enters the transformation queue. The HTTP layer (:mod:`t0.api.upload`)
    decides whether to provide a real one — otherwise the manual upload would stay
    archived but invisible to ``t0.transform``, exactly the problem migration 012
    solved on the schema side.

    Insert happens AFTER the file is written and fsync'd to disk by
    :func:`~t0.fit.archive.archive_download` — never before: see the invariant
    documented on ``raw.fit_file`` (``migrations/002_raw.sql``, carried through by 012).
    It happens even if validation parsing (next step) fails afterward: an archived-but-
    locally-unparse-able upload must still enter the queue, exactly like a freshly
    downloaded Garmin row — it is ``t0.transform`` that attempts real parsing
    and sets ``parse_error`` if needed, not this function.

    Raises:
        t0.api.errors.PayloadTooLargeError: ``data`` exceeds ``max_upload_bytes``.
        t0.api.errors.HostileArchiveError: ZIP archive with dangerous pattern.
        t0.fit.errors.ArchiveError: unrecognized format, empty/multi-member/
            invalid archive (delegated to :func:`t0.fit.archive.archive_download`).
            Nothing left on disk in this case.
        t0.garmin.raw_store.FitFileShaConflictError: ``conn`` provided and the
            sha256 of uploaded content is already associated in database to a DIFFERENT
            source (real Garmin activity, or distinct manual upload sharing same content).
            File stays archived on disk (no loss), but no row inserted for this request.
    """
    if len(data) > max_upload_bytes:
        raise PayloadTooLargeError(
            f"Upload rejected: {len(data)} bytes received, above bound " f"({max_upload_bytes})."
        )
    if _looks_like_zip(data):
        check_zip_safety(data)

    filename_stem = f"upload-{hashlib.sha256(data).hexdigest()[:16]}"
    archived = archive_download(data, dest_dir, filename_stem)

    fit_file_id: int | None = None
    if conn is not None:
        fit_file_id = insert_fit_file(
            conn,
            garmin_activity_id=None,
            storage_path=str(archived.storage_path),
            sha256=archived.sha256,
            size_bytes=archived.size_bytes,
        )

    payload = archived.storage_path.read_bytes()
    try:
        parsed = _summarize(payload, archived.source_format)
    except SourceParseError as exc:
        return UploadOutcome(
            archived=archived, parsed=None, parse_error=str(exc), fit_file_id=fit_file_id
        )
    return UploadOutcome(
        archived=archived, parsed=parsed, parse_error=None, fit_file_id=fit_file_id
    )
