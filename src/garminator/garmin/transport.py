"""Structural contract of the Garmin transport, for injection.

``garminconnect.Garmin`` (the real network client) satisfies this protocol without
inheriting from it explicitly (structural typing). In tests, a fake transport that
implements it is enough to exercise all of L2 **without any network call**.

Deliberately minimal: only ``login`` and ``download_activity`` are typed explicitly,
because they are the only two methods whose precise signature
:class:`~garminator.garmin.client.GarminClient` needs to know. The ~130 other endpoint
methods (``get_sleep_data``, ``get_activities_by_date``, ...) are invoked dynamically by
:meth:`~garminator.garmin.client.GarminClient.fetch` through ``getattr``: their name is
call data, not a typing dependency — pinning the 132 signatures here would duplicate
``garminconnect``'s source file with nothing keeping the copy in sync.
"""

from __future__ import annotations

from typing import Any, Protocol


class GarminTransport(Protocol):
    """Subset of the ``garminconnect.Garmin`` API used by this client."""

    def login(self, tokenstore: str) -> tuple[str | None, str | None]:
        """Authenticate (or restore a session from the tokenstore) and persist the tokens.

        Mirrors the signature of ``garminconnect.Garmin.login``: returns ``(None, None)``
        on full success, or raises one of ``garminconnect``'s exceptions (never a silent
        return on definitive failure).
        """
        ...

    def download_activity(self, activity_id: str, dl_fmt: Any = ...) -> bytes:
        """Download an activity in the requested format and return the raw bytes.

        For ``ActivityDownloadFormat.ORIGINAL`` the bytes are those of a ZIP archive
        whose content (a single file, ``.fit`` or ``.tcx``) is never presumed by this
        module — see :mod:`garminator.garmin.archive`.
        """
        ...
