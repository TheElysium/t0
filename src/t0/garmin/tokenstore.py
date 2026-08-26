"""Garmin session tokenstore handling.

``garminconnect`` 0.3.2 has **no default tokenstore path** (checked in the installed
source, ``garminconnect/__init__.py`` line 419:
``tokenstore = tokenstore or os.getenv("GARMINTOKENS")``). Without that path, every pod
restart performs a full SSO — exactly the account-lockout risk this batch is meant to
avoid. This module therefore centralises the only decision that matters: "did the
tokenstore already hold a reusable session, or are we going to have to build a new one?"

**What the installed source says**, and why "the directory is non-empty" is not good
enough:

- ``garminconnect/client.py::dump`` and ``::load`` — if the given path is a directory,
  both append ``garmin_tokens.json`` to it. That is the **only** file ``garminconnect``
  reads; any other content of the directory is ignored.
- ``garminconnect/client.py::loads`` reads back three keys (``di_token``,
  ``di_refresh_token``, ``di_client_id``) and fails if ``di_token`` is missing
  (``is_authenticated`` = ``bool(di_token or jwt_web)``, and ``jwt_web`` is never
  reloaded from the file).

An unreadable file, malformed JSON, or an empty ``di_token`` all make ``Garmin.login()``
fall back silently to a credentials SSO (``__init__.py`` lines ~455-462:
``except Exception -> tokens_loaded = False``). Reporting "session restored" in those
cases would make the log lie.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Filename imposed by ``garminconnect/client.py::dump``/``::load`` when they are given
#: a directory.
TOKEN_FILENAME = "garmin_tokens.json"  # noqa: S105 - a filename, not a secret


def token_file(path: Path) -> Path:
    """Path of the token file ``garminconnect`` will actually read under ``path``."""
    return path / TOKEN_FILENAME


def has_loadable_tokens(path: Path) -> bool:
    """``True`` if ``garminconnect`` will be able to reload a session from ``path``.

    Applies exactly the criterion of ``garminconnect/client.py::loads``: a JSON object
    carrying a non-empty ``di_token``. Does **not** guarantee that the token is still
    valid — only that a credentials SSO is not unavoidable up front.
    """
    try:
        raw = token_file(path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("di_token"))


def tokenstore_fingerprint(path: Path) -> str | None:
    """sha256 (hex) of the token file, or ``None`` if it is missing or unreadable.

    Used to answer, **after** ``login()``, the question the wrapper cannot answer any
    other way: "did ``garminconnect`` rewrite the tokens?" It only rewrites them
    (``client.dump``) in two cases — a full credentials SSO, or a token refresh
    (``_refresh_session``). An unchanged fingerprint therefore proves neither happened;
    a changed one signals one of the two, without telling them apart
    (``garminconnect`` exposes nothing that would).

    This is deliberately a hash of the content rather than an ``mtime``: an ``mtime``
    also changes on a rewrite with identical content, and its granularity varies with
    the PVC's filesystem.
    """
    try:
        return hashlib.sha256(token_file(path).read_bytes()).hexdigest()
    except OSError:
        return None


def ensure_tokenstore(path: Path) -> bool:
    """Ensure the tokenstore directory exists, and report whether it carries a session.

    Creates the directory (and its parents) if missing — the pod runs with a read-only
    root filesystem, but this path is meant to point at a writable shared volume
    (``fsGroup: 10001``).

    Returns:
        ``True`` if ``garmin_tokens.json`` exists and carries a usable ``di_token``
        (cf. :func:`has_loadable_tokens`). ``False`` otherwise: the next call to
        :meth:`~t0.garmin.transport.GarminTransport.login` will trigger a full
        credentials SSO.
    """
    had_tokens = has_loadable_tokens(path)
    path.mkdir(parents=True, exist_ok=True)
    return had_tokens
