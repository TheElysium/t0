"""Doublures utilisées par les tests du lot L2 : aucune n'ouvre de connexion réseau ou DB.

``FakeTransport`` reproduit le comportement de ``garminconnect.Garmin.login`` qui importe
pour ce lot, tel que lu dans la source installée : les jetons sont cherchés dans le seul
fichier ``garmin_tokens.json`` et ne sont considérés rechargeables que s'ils portent un
``di_token``. Faute de quoi, un SSO complet par identifiants a lieu et **réécrit** le
fichier de jetons (``client.dump``). Le reste (issue de requêtes, téléchargements) est
piloté explicitement par le test via des files de réponses/erreurs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from garminator.garmin.tokenstore import has_loadable_tokens, token_file

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class FakeTransport:
    """Faux transport Garmin : aucune requête HTTP, tout est en mémoire.

    - ``login_outcomes`` : si fourni, une file d'issues à consommer à chaque appel de
      ``login`` (exception à lever, ou ``None`` pour un succès). Sinon, ``login`` simule
      le comportement réel de ``garminconnect`` : pas de ``garmin_tokens.json``
      exploitable -> SSO complet, qui écrit un jeton neuf ; jetons exploitables ->
      ne touche à rien (jetons réutilisés, fichier inchangé).
    - ``responses`` : mappe un nom d'endpoint vers une valeur de retour fixe.
    - ``queued_outcomes`` : mappe un nom d'endpoint vers une file d'issues (exception à
      lever ou valeur à retourner), consommée dans l'ordre — sert à simuler une séquence
      429 -> 429 -> succès.
    """

    login_outcomes: list[BaseException | None] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)
    queued_outcomes: dict[str, list[Any]] = field(default_factory=dict)
    download_outcomes: list[Any] = field(default_factory=list)

    login_calls: list[str] = field(default_factory=list, init=False)
    call_counts: dict[str, int] = field(default_factory=dict, init=False)
    download_calls: list[tuple[str, Any]] = field(default_factory=list, init=False)
    did_full_sso: bool | None = field(default=None, init=False)
    sso_count: int = field(default=0, init=False)

    def login(self, tokenstore: str) -> tuple[str | None, str | None]:
        self.login_calls.append(tokenstore)

        if self.login_outcomes:
            outcome = self.login_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return (None, None)

        path = Path(tokenstore)
        if has_loadable_tokens(path):
            # garminconnect recharge les jetons et ne réécrit pas le fichier.
            self.did_full_sso = False
            return (None, None)

        # SSO complet par identifiants : garminconnect appelle ``dump`` et réécrit
        # ``garmin_tokens.json``. Le compteur rend le contenu (et donc l'empreinte)
        # différent à chaque SSO, comme un vrai jeton fraîchement émis.
        self.sso_count += 1
        path.mkdir(parents=True, exist_ok=True)
        token_file(path).write_text(
            json.dumps({"di_token": f"faux-jeton-{self.sso_count}"}), encoding="utf-8"
        )
        self.did_full_sso = True
        return (None, None)

    def _next(self, endpoint: str) -> Any:
        self.call_counts[endpoint] = self.call_counts.get(endpoint, 0) + 1
        queue = self.queued_outcomes.get(endpoint)
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return self.responses[endpoint]

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Résout dynamiquement tout nom de méthode d'endpoint (get_sleep_data, etc.)
        # comme le fait GarminClient.fetch via getattr() sur le vrai transport.
        def _method(*_args: object, **_kwargs: object) -> Any:
            return self._next(name)

        return _method

    def download_activity(self, activity_id: str, dl_fmt: Any = None) -> bytes:
        self.download_calls.append((activity_id, dl_fmt))
        if self.download_outcomes:
            outcome = self.download_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return bytes(outcome)
        raise AssertionError(
            "FakeTransport.download_activity appelé sans download_outcomes configurés"
        )


class FakeCursor:
    """Curseur en mémoire : enregistre chaque requête, simule ``RETURNING``."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.last_row: tuple[Any, ...] | None = None

    def execute(self, query: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(query.split()), params))
        normalized = " ".join(query.split()).upper()
        if "RETURNING ID" in normalized:
            self._conn.next_api_response_id += 1
            self.last_row = (self._conn.next_api_response_id,)
        elif "RETURNING INGEST_RUN_ID" in normalized:
            self._conn.next_ingest_run_id += 1
            self.last_row = (self._conn.next_ingest_run_id,)
        else:
            self.last_row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.last_row

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass
class FakeConnection:
    """Connexion DB en mémoire satisfaisant ``garminator.garmin.raw_store.DBConnection``."""

    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    next_api_response_id: int = 100
    next_ingest_run_id: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1
