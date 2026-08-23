"""Preuve que le garde-fou réseau de ``conftest.py`` couvre bien les deux piles HTTP.

Ces tests existent parce que la version initiale du garde-fou ne neutralisait que
``socket.socket`` — ce qui laisse passer ``curl_cffi`` (libcurl ouvre ses sockets en
natif), soit 3 des 5 stratégies d'authentification de ``garminconnect``, donc exactement
le chemin qui peut faire bloquer le compte. Si quelqu'un affaiblit ``conftest.py``, ces
tests tombent.

Aucune adresse externe n'apparaît ici : les URL visées sont ``127.0.0.1`` sur un port
fermé, et de toute façon l'appel doit lever **avant** toute tentative de connexion.
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
