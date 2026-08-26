"""Volume archiving of source files (FIT or TCX): write-verify-before-db-row.

Atomicity rule (migrations/002_raw.sql, ``raw.fit_file`` comment): file written AND
verified on volume BEFORE corresponding DB row written. Module stops at first half
(write + verify)--never touches DB (see scope note in batch end report). Order nonetheless
keeps caller safe: if :func:`archive_bytes` raises, NO file left on volume (temp name
cleaned), so caller must never insert row--no file, no row inserted.

Single entry point for both formats: Garmin "original" download returns ZIP archive
with ``.fit`` or ``.tcx`` content (addendum §2.3). :func:`archive_download` sniffs
actual content rather than trusting announced extension.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from t0.fit.errors import ArchiveError

if TYPE_CHECKING:
    from pathlib import Path

#: FIT signature: after variable-length header (typically 12-14 bytes), bytes 8-11 always
#: hold ".FIT" ASCII (verified on running.fit).
_FIT_SIGNATURE = b".FIT"
_FIT_SIGNATURE_OFFSET = 8

#: Standard ZIP signatures: normal file (PK\x03\x04) and empty archive (PK\x05\x06).
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    """Archived file metadata, ready to become ``raw.fit_file`` row.

    Field names intentionally match ``raw.fit_file`` (migrations/002_raw.sql):
    ``sha256``, ``size_bytes``. ``source_format`` has no dedicated column in current
    frozen DDL--encoded in ``storage_path`` extension to stay recoverable without schema change.
    """

    storage_path: Path
    sha256: bytes
    size_bytes: int
    source_format: str


def sniff_format(data: bytes) -> str | None:
    """Determine if bytes are FIT or TCX by content, not extension.

    Returns:
        ``"fit"``, ``"tcx"``, or ``None`` if no signature recognized.
    """
    if (
        len(data) >= _FIT_SIGNATURE_OFFSET + 4
        and data[_FIT_SIGNATURE_OFFSET : _FIT_SIGNATURE_OFFSET + 4] == _FIT_SIGNATURE
    ):
        return "fit"
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or b"TrainingCenterDatabase" in data[:2048]:
        return "tcx"
    return None


def _is_zip(data: bytes) -> bool:
    return any(data[: len(sig)] == sig for sig in _ZIP_SIGNATURES)


#: Bound on **uncompressed** archive member content. Even ultra-distance FIT or TCX
#: fits well below this.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

_MEMBER_CHUNK_BYTES = 1024 * 1024


def _read_member_capped(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, max_uncompressed_bytes: int
) -> bytes:
    """Decompress member in chunks, refusing to exceed bound.

    Why not ``archive.read(member)``: decompresses entire member to memory BEFORE any
    check. Metadata pre-inspection doesn't protect: ``ZipInfo.file_size`` is DECLARED
    in central header, unauthenticated--archive can claim 1 KB, deliver gigabytes.
    ``zipfile`` only notices at end, memory already consumed. Reading in chunks is
    the only real guard.

    Raises:
        ArchiveError: decompressed content exceeds bound.
    """
    chunks: list[bytes] = []
    total = 0
    with archive.open(member) as handle:
        while True:
            chunk = handle.read(_MEMBER_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_uncompressed_bytes:
                raise ArchiveError(
                    f"ZIP archive rejected: member '{member.filename}' exceeds "
                    f"{max_uncompressed_bytes} decompressed bytes (declared size: "
                    f"{member.file_size}). Signature of a decompression bomb."
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_single_member(
    data: bytes, max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES
) -> bytes:
    """Extract sole file from Garmin download ZIP archive.

    Raises:
        ArchiveError: empty archive, corrupt, multi-member (Garmin "original" download
            documented as single-file; multi-member is anomaly for manual handling, not
            guessing), or member exceeds ``max_uncompressed_bytes`` when decompressed.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if len(members) == 0:
                raise ArchiveError("Empty ZIP archive: no file to archive.")
            if len(members) > 1:
                names = ", ".join(m.filename for m in members)
                raise ArchiveError(
                    f"ZIP archive has {len(members)} files, exactly one expected: {names}."
                )
            return _read_member_capped(archive, members[0], max_uncompressed_bytes)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Invalid ZIP archive: {exc}") from exc


def archive_bytes(
    data: bytes,
    dest_dir: Path,
    filename: str,
    *,
    expected_sha256: bytes | None = None,
) -> ArchivedFile:
    """Write ``data`` to ``dest_dir/filename``, verify, then atomic rename.

    Sequence: temp-name write → fsync → re-read → sha256 recalculated from bytes read
    (not from ``data`` in memory, to catch write-time corruption) → compare against
    ``expected_sha256`` if given → atomic ``os.replace``. Any step failure deletes temp
    file: no partial file left on volume.

    Raises:
        ArchiveError: content unrecognized (not FIT or TCX), sha256 mismatch, or I/O error
            during write/re-read.
    """
    source_format = sniff_format(data)
    if source_format is None:
        raise ArchiveError(
            "Content recognized as neither FIT nor TCX (no signature): archiving refused."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / filename
    tmp_path = dest_dir / f".{filename}.tmp-{os.getpid()}-{uuid4().hex}"

    try:
        with tmp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        written = tmp_path.read_bytes()
        if len(written) != len(data):
            raise ArchiveError(
                f"Re-read size ({len(written)}) differs from source size "
                f"({len(data)}): suspicious write, rejected."
            )
        digest = hashlib.sha256(written).digest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ArchiveError(
                "sha256 recomputed after write does not match the expected sha256: "
                "file rejected, no row must be inserted."
            )
        os.replace(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return ArchivedFile(
        storage_path=final_path,
        sha256=digest,
        size_bytes=len(written),
        source_format=source_format,
    )


def extract_payload(
    data: bytes, max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES
) -> bytes:
    """Bytes actually archivable from Garmin download: ZIP member or ``data``.

    Public (not internal to ``archive_download``) because caller needs to hash THIS content
    before deciding if worth archiving--exactly :mod:`t0.scheduler.activity_files`
    case, comparing download sha256 against latest version in DB (``raw.fit_file.sha256``).
    But that column holds sha of EXTRACTED member, what ``archive_bytes`` calculates post-reread.
    Hashing ZIP envelope instead can never match: comparison fails systematically, idempotence
    silently lost (real defect, fixed in review). Single source of truth: "what are we
    archiving", shared by both callers.

    Raises:
        ArchiveError: empty, corrupt, multi-member ZIP, or decompression bomb
            (see :func:`_extract_single_member`).
    """
    return _extract_single_member(data, max_uncompressed_bytes) if _is_zip(data) else data


def archive_download(
    data: bytes,
    dest_dir: Path,
    filename_stem: str,
    *,
    expected_sha256: bytes | None = None,
) -> ArchivedFile:
    """Sole entry point for Garmin "original" download.

    Sniffs whether ``data`` is ZIP archive (documented Garmin download case) and if so,
    extracts sole member before archiving. Never assumes format from URL extension:
    content alone decides via :func:`sniff_format`. Archived file extension (``.fit``/``.tcx``)
    derived from sniff, never from caller's expectation.
    """
    payload = extract_payload(data)
    fmt = sniff_format(payload)
    if fmt is None:
        raise ArchiveError(
            "Downloaded content recognized as neither FIT nor TCX: archiving refused."
        )
    return archive_bytes(
        payload, dest_dir, f"{filename_stem}.{fmt}", expected_sha256=expected_sha256
    )
