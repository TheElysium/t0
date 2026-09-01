"""MCP server assembly: seven curated tools over the marts, streamable HTTP at /mcp."""

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

# Reached through the ClusterIP Service / Gateway HTTPRoute, not localhost.
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
        # Host-header-based DNS rebinding protection would reject every
        # Gateway-proxied request; the client is the CLI with a Bearer header.
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
        rest days are rows, not absences. Chronological; with no date range,
        the LATEST max_rows days."""
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
        baselines and deviations -- deviations are the actionable quantity.
        Chronological; with no date range, the LATEST max_rows nights."""
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
        triggering numbers in detail. Chronological; with no date range, the
        LATEST max_rows days. Advisory only, never a decision."""
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
    """Assemble the ASGI app: MCP at /mcp, probes beside it, Bearer in front."""
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
