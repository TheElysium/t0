"""Tests de :mod:`garminator.garmin.tokenstore`.

Le critere teste ici n'est pas « le repertoire est non vide » mais celui qu'applique
reellement ``garminconnect/client.py::loads`` : un fichier ``garmin_tokens.json`` portant
un ``di_token`` non vide. Tout le reste (autre nom de fichier, JSON casse, ``di_token``
absent) fait retomber ``Garmin.login()`` sur un SSO complet par identifiants, silencieux.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from garminator.garmin.tokenstore import (
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
    # L'appel ne doit rien detruire.
    assert (target / TOKEN_FILENAME).exists()


def test_directory_populated_with_another_file_is_not_loadable(tmp_path: Path) -> None:
    """Un repertoire non vide ne suffit pas : garminconnect ne lit que garmin_tokens.json."""
    target = tmp_path / "leurre"
    target.mkdir()
    (target / "oauth2_token.json").write_text("{}", encoding="utf-8")

    assert has_loadable_tokens(target) is False
    assert ensure_tokenstore(target) is False


def test_token_file_without_di_token_is_not_loadable(tmp_path: Path) -> None:
    """``loads`` echoue si ``di_token`` manque -> SSO complet inevitable."""
    _write_tokens(tmp_path, {"di_refresh_token": "seul"})

    assert has_loadable_tokens(tmp_path) is False


def test_corrupt_token_file_is_not_loadable(tmp_path: Path) -> None:
    (tmp_path / TOKEN_FILENAME).write_text("{ pas du json", encoding="utf-8")

    assert has_loadable_tokens(tmp_path) is False


def test_fingerprint_is_none_when_absent_and_stable_when_unchanged(tmp_path: Path) -> None:
    assert tokenstore_fingerprint(tmp_path) is None

    _write_tokens(tmp_path, {"di_token": "abc"})
    first = tokenstore_fingerprint(tmp_path)

    assert first is not None
    assert tokenstore_fingerprint(tmp_path) == first


def test_fingerprint_changes_when_tokens_are_rewritten(tmp_path: Path) -> None:
    """C'est la seule chose observable d'un SSO complet depuis le wrapper."""
    _write_tokens(tmp_path, {"di_token": "abc"})
    before = tokenstore_fingerprint(tmp_path)

    _write_tokens(tmp_path, {"di_token": "xyz"})

    assert tokenstore_fingerprint(tmp_path) != before
