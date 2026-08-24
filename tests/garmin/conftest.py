"""Shared fixtures for batch L2 tests, and proof no test does network.

``block_all_network`` is ``autouse`` on this entire test package.

**Why it's not enough to mock ``socket.socket``.** ``garminconnect`` does not use only
``requests``: its authentication strategy chain uses ``curl_cffi`` (3 of 5 strategies),
i.e., libcurl via CFFI. libcurl opens sockets **natively**, never through Python's
``socket`` module. Verified experimentally: with ``socket.socket`` mocked, ``curl_cffi``
still establishes a connection (the observed failure came from closed port, not the
guard). A guard limited to ``socket.socket`` left precisely the SSO path — the most
dangerous, the one that can lock out a Garmin account — entirely open.

So we block three doors at their respective entry points:

1. ``socket.socket`` — all pure-Python network (including psycopg native mode);
2. ``curl_cffi.curl.Curl.perform`` — the only method triggering a libcurl transfer,
   regardless of API level (``Session``, ``requests.get``, …);
3. ``requests.adapters.HTTPAdapter.send`` — belt and suspenders for ``requests``,
   whose connection pooling could reuse a pre-existing socket.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


class NetworkAccessDeniedError(RuntimeError):
    """Raised when code under test attempts network access."""


def _deny(what: str) -> object:
    raise NetworkAccessDeniedError(
        f"tests/garmin: network access attempt intercepted ({what}) — no test in this "
        "batch must contact a real service (Garmin or otherwise)."
    )


@pytest.fixture(autouse=True)
def block_all_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block all network access during ``tests/garmin`` tests."""

    def _forbidden_socket(*_args: object, **_kwargs: object) -> object:
        return _deny("socket.socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)

    # curl_cffi (libcurl via CFFI): native sockets, invisible to socket module.
    # Import must precede socket.socket patch in a fresh process, but here pytest has
    # already imported everything by the time the fixture runs.
    try:
        import curl_cffi.curl
    except ImportError:  # pragma: no cover - dependency present in practice
        pass
    else:

        def _forbidden_perform(*_args: object, **_kwargs: object) -> object:
            return _deny("curl_cffi.Curl.perform")

        monkeypatch.setattr(curl_cffi.curl.Curl, "perform", _forbidden_perform)

    try:
        import requests.adapters
    except ImportError:  # pragma: no cover - dependency present in practice
        pass
    else:

        def _forbidden_send(*_args: object, **_kwargs: object) -> object:
            return _deny("requests.HTTPAdapter.send")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _forbidden_send)

    yield
