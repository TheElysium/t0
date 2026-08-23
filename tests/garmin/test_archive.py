"""Tests de :mod:`garminator.garmin.archive`.

Vérifie qu'aucun format n'est présumé d'après l'URL : le fichier interne peut être
``.fit`` ou ``.tcx`` (addendum athlète §2), seul le nom réel présent dans le ZIP compte.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from garminator.garmin.archive import ArchiveError, extract_original_filename


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extracts_fit_filename() -> None:
    archive = _zip_bytes({"12345678.fit": b"fake fit bytes"})

    assert extract_original_filename(archive) == "12345678.fit"


def test_extracts_tcx_filename() -> None:
    archive = _zip_bytes({"98765432.tcx": b"<TrainingCenterDatabase/>"})

    assert extract_original_filename(archive) == "98765432.tcx"


def test_empty_archive_raises() -> None:
    archive = _zip_bytes({})

    with pytest.raises(ArchiveError, match="vide"):
        extract_original_filename(archive)


def test_multi_file_archive_raises() -> None:
    archive = _zip_bytes({"a.fit": b"1", "b.fit": b"2"})

    with pytest.raises(ArchiveError, match="plusieurs fichiers"):
        extract_original_filename(archive)


def test_non_zip_bytes_raise_archive_error() -> None:
    with pytest.raises(ArchiveError, match="ZIP"):
        extract_original_filename(b"not a zip at all")
