"""Tests for :mod:`t0.garmin.tokenstore`.

The criterion tested here is not "directory is non-empty" but what
``garminconnect/client.py::loads`` actually applies: a ``garmin_tokens.json`` file
carrying a non-empty ``di_token``. Everything else (different filename, JSON broken,
``di_token`` missing) makes ``Garmin.login()`` fall through to full SSO via credentials,
silently.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from t0.garmin.tokenstore import (
    TOKEN_FILENAME,
    ensure_tokenstore,
    has_loadable_tokens,
    tokenstore_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_tokens(target: Path, payload: object) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / TOKEN_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_ensure_tokenstore_creates_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "does" / "not" / "exist"

    had_tokens = ensure_tokenstore(target)

    assert had_tokens is False
    assert target.is_dir()


def test_ensure_tokenstore_reports_existing_empty_directory_as_no_tokens(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()

    assert ensure_tokenstore(target) is False


def test_ensure_tokenstore_reports_real_token_file_as_having_tokens(tmp_path: Path) -> None:
    target = tmp_path / "populated"
    _write_tokens(target, {"di_token": "abc", "di_refresh_token": "def"})

    assert ensure_tokenstore(target) is True
    # Call must not destroy anything.
    assert (target / TOKEN_FILENAME).exists()


def test_directory_populated_with_another_file_is_not_loadable(tmp_path: Path) -> None:
    """Non-empty directory is not enough: garminconnect reads only garmin_tokens.json."""
    target = tmp_path / "leurre"
    target.mkdir()
    (target / "oauth2_token.json").write_text("{}", encoding="utf-8")

    assert has_loadable_tokens(target) is False
    assert ensure_tokenstore(target) is False


def test_token_file_without_di_token_is_not_loadable(tmp_path: Path) -> None:
    """``loads`` fails if ``di_token`` missing -> inevitable full SSO."""
    _write_tokens(tmp_path, {"di_refresh_token": "seul"})

    assert has_loadable_tokens(tmp_path) is False


def test_corrupt_token_file_is_not_loadable(tmp_path: Path) -> None:
    (tmp_path / TOKEN_FILENAME).write_text("{ not json", encoding="utf-8")

    assert has_loadable_tokens(tmp_path) is False


def test_fingerprint_is_none_when_absent_and_stable_when_unchanged(tmp_path: Path) -> None:
    assert tokenstore_fingerprint(tmp_path) is None

    _write_tokens(tmp_path, {"di_token": "abc"})
    first = tokenstore_fingerprint(tmp_path)

    assert first is not None
    assert tokenstore_fingerprint(tmp_path) == first


def test_fingerprint_changes_when_tokens_are_rewritten(tmp_path: Path) -> None:
    """This is the only thing observable of a full SSO from the wrapper."""
    _write_tokens(tmp_path, {"di_token": "abc"})
    before = tokenstore_fingerprint(tmp_path)

    _write_tokens(tmp_path, {"di_token": "xyz"})

    assert tokenstore_fingerprint(tmp_path) != before
