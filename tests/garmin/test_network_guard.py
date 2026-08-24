"""Proof that network guard in ``conftest.py`` covers both HTTP stacks.

These tests exist because the initial guard version only mocked ``socket.socket`` —
which lets ``curl_cffi`` through (libcurl opens sockets natively), i.e., 3 of 5
``garminconnect`` auth strategies, exactly the path that can lock out an account.
If someone weakens ``conftest.py``, these tests fail.

No external address appears here: targeted URLs are ``127.0.0.1`` on closed port, and
the call must raise **before** any connection attempt anyway.
"""

from __future__ import annotations

import socket

import pytest

from tests.garmin.conftest import NetworkAccessDeniedError

LOCAL_URL = "http://127.0.0.1:1/"


def test_stdlib_socket_is_blocked() -> None:
    with pytest.raises(NetworkAccessDeniedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_curl_cffi_is_blocked() -> None:
    curl_cffi = pytest.importorskip("curl_cffi")

    with pytest.raises(NetworkAccessDeniedError):
        curl_cffi.requests.get(LOCAL_URL, timeout=1)


def test_requests_is_blocked() -> None:
    requests = pytest.importorskip("requests")

    with pytest.raises(NetworkAccessDeniedError):
        requests.get(LOCAL_URL, timeout=1)
