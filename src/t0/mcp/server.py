"""MCP server assembly: seven curated tools over the marts, streamable HTTP.

Shape (HANDOFF-MCP §1/§2): a FastMCP server in stateless session mode, served
as a Starlette ASGI app at ``/mcp`` (streamable HTTP), wrapped in the Bearer
middleware (:mod:`t0.mcp.auth`), with ``/healthz`` and ``/readyz`` beside it
for the chart probes. Plain HTTP on the LAN is deliberate, exactly like
Grafana; TLS is the Gateway's business.

No raw-SQL escape hatch, no write tool: the seven handlers in
:mod:`t0.mcp.handlers` are the entire query surface, and the database role is
read-only on top of that. Refusing startup without a token or DSN is done at
assembly time (:func:`create_app`) -- a misconfigured server must fail loudly
instead of serving unauthenticated or on the wrong credentials.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from starlette.routing import Route

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from t0.mcp import handlers
from t0.mcp.auth import BearerTokenMiddleware
from t0.mcp.health import build_readyz, healthz
from t0.mcp.instructions import INSTRUCTIONS

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from t0.mcp.db import Reader
    from t0.settings import Settings

logger = logging.getLogger(__name__)

#: The MCP binds every interface: it is reached through the ClusterIP Service /
#: Gateway HTTPRoute, not through a sidecar localhost. Kubernetes decides what
#: reaches it (S104 therefore does not apply: there is no direct LAN exposure
#: of a pod).
BIND_HOST = "0.0.0.0"  # noqa: S104


class McpConfigError(RuntimeError):
    """MCP-specific configuration missing. Message names variables, never values."""


def create_mcp_server(settings: Settings, reader: Reader) -> FastMCP:
    """Build the FastMCP server with exactly the seven curated tools."""
    mcp = FastMCP(
        "t0",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        host=BIND_HOST,
        port=settings.mcp_port,
        # DNS rebinding protection is auto-enabled for localhost binds and
        # checks the Host header: it would reject every request proxied by the
        # Gateway under t0-mcp.homelab.internal. The client here is the Claude
        # Code CLI with a static header, not a browser: the mitigation does not
        # apply, authentication is the Bearer middleware's job.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    max_rows = settings.mcp_max_rows

    @mcp.tool()
    async def get_pmc(
        start_date: str | None = None,
        end_date: str | None = None,
        athlete_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Performance Management Chart: daily CTL (42 d), ATL (7 d), TSB,
        with ctl_reliable and has_estimated_load flags. Dense calendar --
        rest days are rows, not absences."""
        return await asyncio.to_thread(
            handlers.get_pmc,
            reader,
            max_rows=max_rows,
            athlete_id=athlete_id,
            start_date=start_date,
            end_date=end_date,
        )

    @mcp.tool()
    async def list_activities(
        start_date: str | None = None,
        end_date: str | None = None,
        sport_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent sessions, newest first, with tss, tss_method,
        intensity_factor, data_quality and is_parent. Multisport parents are
        never returned: count legs, never legs + parent."""
        return await asyncio.to_thread(
            handlers.list_activities,
            reader,
            max_rows=max_rows,
            start_date=start_date,
            end_date=end_date,
            sport_code=sport_code,
        )

    @mcp.tool()
    async def get_activity(activity_id: int) -> dict[str, Any] | None:
        """One session by id, all wide-view metrics (HR, power, NP, IF,
        decoupling, efficiency) with their caveat flags."""
        return await asyncio.to_thread(handlers.get_activity, reader, activity_id=activity_id)

    @mcp.tool()
    async def get_power_curve(kind: str | None = None) -> list[dict[str, Any]]:
        """All-time record curve (best value per duration), by curve type;
        date = the day the record was set."""
        return await asyncio.to_thread(
            handlers.get_power_curve, reader, max_rows=max_rows, kind=kind
        )

    @mcp.tool()
    async def get_wellness(
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Nightly HRV and resting HR with their 28-day rolling median
        baselines and deviations -- deviations are the actionable quantity."""
        return await asyncio.to_thread(
            handlers.get_wellness,
            reader,
            max_rows=max_rows,
            start_date=start_date,
            end_date=end_date,
        )

    @mcp.tool()
    async def get_coach_flags(
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fired amber/red coaching rules, one row per (day, rule), with the
        triggering numbers in detail. Advisory only, never a decision."""
        return await asyncio.to_thread(
            handlers.get_coach_flags,
            reader,
            max_rows=max_rows,
            start_date=start_date,
            end_date=end_date,
        )

    @mcp.tool()
    async def get_thresholds(discipline: str | None = None) -> list[dict[str, Any]]:
        """Historized physiological thresholds (FTP, rFTP, pace, CSS, LTHR) by
        validity period, with css_estimated flagged."""
        return await asyncio.to_thread(
            handlers.get_thresholds, reader, max_rows=max_rows, discipline=discipline
        )

    return mcp


def create_app(settings: Settings, reader: Reader) -> ASGIApp:
    """Assemble the ASGI app: MCP at /mcp, probes beside it, Bearer in front.

    Raises:
        McpConfigError: when the Bearer token or the read-only DSN is absent.
            The server refuses to start rather than serve unauthenticated or
            fall back to the ingest credentials. Messages cite variable names
            only, never values.
    """
    if settings.mcp_bearer_token is None:
        raise McpConfigError("T0_MCP_BEARER_TOKEN is required: refusing to start unauthenticated.")
    if settings.mcp_postgres_dsn is None:
        raise McpConfigError(
            "T0_MCP_POSTGRES_DSN is required: the MCP uses the read-only role "
            "t0_mcp_ro, never the ingest credentials."
        )

    mcp = create_mcp_server(settings, reader)
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
    app.router.routes.append(Route("/readyz", build_readyz(reader), methods=["GET"]))
    return BearerTokenMiddleware(app, settings.mcp_bearer_token.get_secret_value())


__all__ = ["McpConfigError", "create_app", "create_mcp_server"]
