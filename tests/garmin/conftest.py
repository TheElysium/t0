"""Fixtures partagées des tests du lot L2, et preuve qu'aucun test ne fait de réseau.

``block_all_network`` est ``autouse`` sur tout ce paquet de tests.

**Pourquoi il ne suffit pas de neutraliser ``socket.socket``.** ``garminconnect``
n'utilise pas uniquement ``requests`` : sa chaîne de stratégies d'authentification
emploie ``curl_cffi`` (3 stratégies sur 5), c'est-à-dire libcurl appelé via CFFI. libcurl
ouvre ses sockets **en natif**, sans jamais passer par le module ``socket`` de Python.
Vérifié expérimentalement : avec ``socket.socket`` neutralisé, ``curl_cffi`` établit
quand même une connexion (l'échec observé venait du port fermé, pas du garde-fou). Un
garde-fou limité à ``socket.socket`` laissait donc précisément le chemin SSO — le plus
dangereux, celui qui peut faire bloquer le compte Garmin — entièrement ouvert.

On barre donc les trois portes, à leur point d'entrée respectif :

1. ``socket.socket`` — tout le réseau en Python pur (dont psycopg en mode natif) ;
2. ``curl_cffi.curl.Curl.perform`` — la seule méthode qui déclenche un transfert libcurl,
   quel que soit le niveau d'API (``Session``, ``requests.get``, …) ;
3. ``requests.adapters.HTTPAdapter.send`` — ceinture et bretelles pour ``requests``,
   dont la mise en commun de connexions pourrait réutiliser un socket préexistant.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


class NetworkAccessDeniedError(RuntimeError):
    """Levée quand du code sous test tente d'atteindre le réseau."""


def _deny(what: str) -> object:
    raise NetworkAccessDeniedError(
        f"tests/garmin : tentative d'accès réseau interceptée ({what}) — aucun test de "
        "ce lot ne doit contacter un service réel (Garmin ou autre)."
    )


@pytest.fixture(autouse=True)
def block_all_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Interdit tout accès réseau pendant les tests de ``tests/garmin``."""

    def _forbidden_socket(*_args: object, **_kwargs: object) -> object:
        return _deny("socket.socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)

    # curl_cffi (libcurl via CFFI) : sockets natifs, invisibles du module socket.
    # L'import doit précéder le patch de socket.socket dans un processus neuf, mais ici
    # pytest a déjà tout importé au moment où la fixture s'exécute.
    try:
        import curl_cffi.curl
    except ImportError:  # pragma: no cover - dépendance présente en pratique
        pass
    else:

        def _forbidden_perform(*_args: object, **_kwargs: object) -> object:
            return _deny("curl_cffi.Curl.perform")

        monkeypatch.setattr(curl_cffi.curl.Curl, "perform", _forbidden_perform)

    try:
        import requests.adapters
    except ImportError:  # pragma: no cover - dépendance présente en pratique
        pass
    else:

        def _forbidden_send(*_args: object, **_kwargs: object) -> object:
            return _deny("requests.HTTPAdapter.send")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _forbidden_send)

    yield
