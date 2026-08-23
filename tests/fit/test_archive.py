"""Tests of garminator.fit.archive: atomic write, verify, no side effects.

Batch acceptance requirement: test demonstrates corrupt file leaves no side effects
(no orphan files on volume).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from garminator.fit.archive import (
    ArchivedFile,
    archive_bytes,
    archive_download,
    sniff_format,
)
from garminator.fit.errors import ArchiveError

if TYPE_CHECKING:
    from pathlib import Path

_FAKE_FIT = b"\x0e\x10\xc0\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20
_FAKE_TCX = b'<?xml version="1.0"?><TrainingCenterDatabase/>'


def test_sniff_format_fit_by_magic_bytes() -> None:
    assert sniff_format(_FAKE_FIT) == "fit"


def test_sniff_format_tcx_by_xml_declaration() -> None:
    assert sniff_format(_FAKE_TCX) == "tcx"


def test_sniff_format_unrecognized_returns_none() -> None:
    assert sniff_format(b"not a known format at all") is None


def test_archive_bytes_writes_verifies_and_renames(tmp_path: Path) -> None:
    result = archive_bytes(_FAKE_FIT, tmp_path, "12345.fit")

    assert isinstance(result, ArchivedFile)
    assert result.storage_path == tmp_path / "12345.fit"
    assert result.storage_path.exists()
    assert result.storage_path.read_bytes() == _FAKE_FIT
    assert result.sha256 == hashlib.sha256(_FAKE_FIT).digest()
    assert result.size_bytes == len(_FAKE_FIT)
    assert result.source_format == "fit"
    # No temp file should remain after success.
    leftovers = list(tmp_path.iterdir())
    assert leftovers == [result.storage_path]


def test_archive_bytes_rejects_unrecognized_content_with_no_side_effect(
    tmp_path: Path,
) -> None:
    with pytest.raises(ArchiveError):
        archive_bytes(b"garbage bytes, not FIT nor TCX", tmp_path, "bad.bin")

    # No file—neither final nor temp—should be left.
    assert list(tmp_path.iterdir()) == []


def test_archive_bytes_rejects_sha256_mismatch_with_no_side_effect(tmp_path: Path) -> None:
    wrong_digest = hashlib.sha256(b"autre chose").digest()

    with pytest.raises(ArchiveError, match="sha256"):
        archive_bytes(_FAKE_FIT, tmp_path, "12345.fit", expected_sha256=wrong_digest)

    assert list(tmp_path.iterdir()) == []


def test_archive_bytes_never_leaves_temp_file_on_directory_creation_reuse(
    tmp_path: Path,
) -> None:
    # Two successive writes should leave only one final file, never accumulated temp residue.
    archive_bytes(_FAKE_FIT, tmp_path, "a.fit")
    archive_bytes(_FAKE_TCX, tmp_path, "b.tcx")

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["a.fit", "b.tcx"]


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_archive_download_extracts_fit_from_zip(tmp_path: Path) -> None:
    zip_data = _zip_bytes({"98765.fit": _FAKE_FIT})

    result = archive_download(zip_data, tmp_path, "activity-98765")

    assert result.source_format == "fit"
    assert result.storage_path.name == "activity-98765.fit"
    assert result.storage_path.read_bytes() == _FAKE_FIT


def test_archive_download_extracts_tcx_from_zip(tmp_path: Path) -> None:
    zip_data = _zip_bytes({"11111.tcx": _FAKE_TCX})

    result = archive_download(zip_data, tmp_path, "activity-11111")

    assert result.source_format == "tcx"
    assert result.storage_path.name == "activity-11111.tcx"


def test_archive_download_handles_direct_fit_without_zip_wrapper(tmp_path: Path) -> None:
    result = archive_download(_FAKE_FIT, tmp_path, "direct")
    assert result.source_format == "fit"


def test_archive_download_rejects_empty_zip_with_no_side_effect(tmp_path: Path) -> None:
    zip_data = _zip_bytes({})

    with pytest.raises(ArchiveError, match="vide"):
        archive_download(zip_data, tmp_path, "empty")

    assert list(tmp_path.iterdir()) == []


def test_archive_download_rejects_multi_member_zip(tmp_path: Path) -> None:
    zip_data = _zip_bytes({"a.fit": _FAKE_FIT, "b.fit": _FAKE_FIT})

    with pytest.raises(ArchiveError, match="fichiers"):
        archive_download(zip_data, tmp_path, "multi")

    assert list(tmp_path.iterdir()) == []


def test_archive_download_rejects_malformed_zip_with_no_side_effect(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="invalide"):
        archive_download(b"PK\x03\x04not a real zip stream", tmp_path, "corrupt")

    assert list(tmp_path.iterdir()) == []


def test_decompression_bounded_by_actual_content_not_claimed_size() -> None:
    """Decompression bomb guard, right place.

    Metadata pre-inspection insufficient: ``ZipInfo.file_size`` and compression ratio
    are DECLARED values hostile archive chooses. Test uses incompressible payload—ratio ~1,
    claimed size exact, innocent to metadata inspection—verifies block-by-block read stops it.

    Countertest included: SAME archive accepted once bound sufficient, else test passes
    with impl that rejects all.
    """
    import io
    import os
    import zipfile

    from garminator.fit.archive import _extract_single_member

    charge = os.urandom(100_000)  # incompressible: undetectable on metadata
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("activity.fit", charge)
    brut = tampon.getvalue()

    with zipfile.ZipFile(io.BytesIO(brut)) as archive:
        info = archive.infolist()[0]
        assert (
            info.file_size / info.compress_size < 1.1
        ), "setup must produce innocent-ratio archive"

    with pytest.raises(ArchiveError, match="bombe de décompression"):
        _extract_single_member(brut, max_uncompressed_bytes=1_000)

    # Countertest: sufficient bound, same archive accepted intact.
    assert _extract_single_member(brut, max_uncompressed_bytes=1_000_000) == charge
