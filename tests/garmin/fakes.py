"""Test doubles used in batch L2 tests: none open network or DB connections.

``FakeTransport`` reproduces ``garminconnect.Garmin.login`` behavior relevant to this
batch, as read from installed source: tokens are sought only in ``garmin_tokens.json``
and are only considered reloadable if they carry a ``di_token``. Otherwise, full SSO
via credentials occurs and **rewrites** the token file (``client.dump``). The rest
(request results, downloads) is driven explicitly by the test via response/error queues.
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
    """Fake Garmin transport: no HTTP requests, all in-memory.

    - ``login_outcomes``: if provided, a queue of outcomes to consume each ``login``
      call (exception to raise, or ``None`` for success). Otherwise, ``login`` simulates
      real ``garminconnect`` behavior: no usable ``garmin_tokens.json`` -> full SSO,
      writes a new token; usable tokens -> does nothing (tokens reused, file unchanged).
    - ``responses``: maps an endpoint name to a fixed return value.
    - ``queued_outcomes``: maps an endpoint name to a queue of outcomes (exception to
      raise or value to return), consumed in order — simulates sequences like
      429 -> 429 -> success.
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
            # garminconnect reloads tokens and does not rewrite the file.
            self.did_full_sso = False
            return (None, None)

        # Full SSO via credentials: garminconnect calls ``dump`` and rewrites
        # ``garmin_tokens.json``. The counter makes content (and thus fingerprint)
        # different each SSO, like a freshly-issued real token.
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
        # Dynamically resolve any endpoint method name (get_sleep_data, etc.)
        # like GarminClient.fetch does via getattr() on the real transport.
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
            "FakeTransport.download_activity called without download_outcomes configured"
        )


class FakeCursor:
    """In-memory cursor: records each query, simulates ``RETURNING``."""

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
    """In-memory DB connection satisfying ``garminator.garmin.raw_store.DBConnection``."""

    executed: list[tuple[str, Any]] = field(default_factory=list)
    commit_count: int = 0
    next_api_response_id: int = 100
    next_ingest_run_id: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1
