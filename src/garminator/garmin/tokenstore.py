"""Gestion du tokenstore de session Garmin.

``garminconnect`` 0.3.2 n'a **aucun chemin de tokenstore par défaut** (vérifié dans la
source installée, ``garminconnect/__init__.py`` ligne 419 :
``tokenstore = tokenstore or os.getenv("GARMINTOKENS")``). Sans ce chemin, chaque
redémarrage de pod refait un SSO complet — le risque de blocage de compte que ce lot
cherche précisément à éviter. Ce module centralise donc la seule décision qui compte :
« le tokenstore contenait-il déjà une session réutilisable, ou allons-nous devoir en
refaire une ? »

**Ce que dit la source installée**, et qui interdit de se contenter d'un « le répertoire
est non vide » :

- ``garminconnect/client.py::dump`` et ``::load`` — si le chemin fourni est un
  répertoire, tous deux le complètent par ``garmin_tokens.json``. C'est **le seul**
  fichier que ``garminconnect`` lit ; n'importe quel autre contenu du répertoire est
  ignoré.
- ``garminconnect/client.py::loads`` relit trois clés (``di_token``,
  ``di_refresh_token``, ``di_client_id``) et échoue si ``di_token`` est absent
  (``is_authenticated`` = ``bool(di_token or jwt_web)``, et ``jwt_web`` n'est jamais
  rechargé depuis le fichier).

Un fichier illisible, un JSON malformé, ou un ``di_token`` vide font que
``Garmin.login()`` bascule silencieusement sur un SSO par identifiants
(``__init__.py`` lignes ~455-462 : ``except Exception -> tokens_loaded = False``).
Rapporter « session restaurée » dans ces cas serait un journal faux.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Nom de fichier imposé par ``garminconnect/client.py::dump``/``::load`` lorsqu'on leur
#: passe un répertoire.
TOKEN_FILENAME = "garmin_tokens.json"  # noqa: S105 - nom de fichier, pas un secret


def token_file(path: Path) -> Path:
    """Chemin du fichier de jetons que ``garminconnect`` lira réellement sous ``path``."""
    return path / TOKEN_FILENAME


def has_loadable_tokens(path: Path) -> bool:
    """``True`` si ``garminconnect`` pourra recharger une session depuis ``path``.

    Applique exactement le critère de ``garminconnect/client.py::loads`` : un JSON objet
    portant un ``di_token`` non vide. Ne garantit **pas** que le jeton soit encore
    valide — seulement qu'un SSO par identifiants n'est pas d'emblée inévitable.
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
    """sha256 (hex) du fichier de jetons, ou ``None`` s'il est absent ou illisible.

    Sert à répondre, **après** ``login()``, à la question à laquelle le wrapper ne peut
    pas répondre autrement : « ``garminconnect`` a-t-il réécrit les jetons ? » Il ne les
    réécrit (``client.dump``) que dans deux cas — un SSO complet par identifiants, ou un
    rafraîchissement de jeton (``_refresh_session``). Une empreinte inchangée prouve donc
    qu'aucun des deux n'a eu lieu ; une empreinte modifiée signale l'un des deux, sans
    permettre de les distinguer (``garminconnect`` n'expose rien qui le permette).

    C'est délibérément un hash du contenu et non une ``mtime`` : une ``mtime`` change
    aussi lors d'une réécriture à contenu identique, et sa granularité varie selon le
    système de fichiers du PVC.
    """
    try:
        return hashlib.sha256(token_file(path).read_bytes()).hexdigest()
    except OSError:
        return None


def ensure_tokenstore(path: Path) -> bool:
    """Garantit que le répertoire du tokenstore existe, et rapporte s'il porte une session.

    Crée le répertoire (et ses parents) s'il est absent — le pod tourne en filesystem
    racine en lecture seule, mais ce chemin est censé pointer sur un volume inscriptible
    partagé (``fsGroup: 10001``).

    Returns:
        ``True`` si ``garmin_tokens.json`` existe et porte un ``di_token`` exploitable
        (cf. :func:`has_loadable_tokens`). ``False`` sinon : le prochain appel à
        :meth:`~garminator.garmin.transport.GarminTransport.login` va déclencher un SSO
        complet par identifiants.
    """
    had_tokens = has_loadable_tokens(path)
    path.mkdir(parents=True, exist_ok=True)
    return had_tokens
