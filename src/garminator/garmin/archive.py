"""Read real filename from original activity download's ZIP archive.

Confirmed by athlete addendum (§2): Garmin activity's "original" download is always a
ZIP archive containing either `.fit` OR `.tcx` depending on whether recorded by watch or
manually imported (home-trainer). Module never presumes format from URL/extension: reads
real name from ZIP central directory.

Intentionally, module does NOT decode internal file content—only ZIP metadata (name,
presence). FIT/TCX decoding is L3's role; this layer delivers raw bytes and real filename.
"""

from __future__ import annotations

import io
import zipfile


class ArchiveError(RuntimeError):
    """Downloaded archive does not match expected form (empty, multiple files, or not ZIP)."""


def extract_original_filename(zip_bytes: bytes) -> str:
    """Name of single file in activity's "original" ZIP archive.

    Raises:
        ArchiveError: if bytes are not valid ZIP, archive is empty, or contains
            multiple files (case not covered by athlete addendum, which documents
            single-file archive `.fit` or `.tcx`).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
    except zipfile.BadZipFile as exc:
        raise ArchiveError("Downloaded content is not a valid ZIP archive.") from exc

    if not names:
        raise ArchiveError("Empty ZIP archive: no files inside.")
    if len(names) > 1:
        raise ArchiveError(f"ZIP archive contains multiple files, unexpected: {names!r}")
    return names[0]
