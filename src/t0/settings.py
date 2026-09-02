"""t0 configuration, read from environment.

Two invariants anchor this module:

1. **No defaults for Garmin credentials, DSN, or GARMINTOKENS path**: without
   explicit env vars, the service refuses to start rather than fall back to
   implicit behavior (e.g., full Garmin SSO on every restart from no persisted
   session—exposes account to lock).
2. **No sensitive value ever leaks to logs or exceptions**. Sensitive fields are
   SecretStr (repr masked), and get_settings() converts ValidationError to cite
   only variable names, never values—pydantic otherwise includes the full input
   dict (including cleartext password) in the error message.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path  # noqa: TC003 — pydantic must resolve type at runtime
from typing import TypeVar

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "T0_"

_SettingsT = TypeVar("_SettingsT", bound="McpSettings")


class SettingsError(RuntimeError):
    """Invalid configuration.

    Never contains environment values: only variable names and error codes.
    """


class McpSettings(BaseSettings):
    """MCP-server parameters: the T0_MCP_* variables and nothing else.

    Distinct class rather than part of Settings: the MCP pod deliberately holds
    no ingest credentials (T0_POSTGRES_DSN, T0_GARMIN_*) -- it must never be
    able to read or to poll Garmin -- so its entrypoint validates this class
    only, and the ingest-only required fields are absent from its environment.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MCP server (src/t0/mcp). All optional: ingest and the scheduler share
    # Settings below; the MCP entrypoint refuses to start when token or DSN
    # are absent.
    mcp_bearer_token: SecretStr | None = Field(
        default=None,
        description="Static Bearer token required by the MCP server (Authorization header).",
    )
    mcp_postgres_dsn: SecretStr | None = Field(
        default=None,
        description=(
            "PostgreSQL DSN of the read-only role t0_mcp_ro, dedicated to the MCP server "
            "(never the ingest role)."
        ),
    )
    mcp_statement_timeout_ms: int = Field(
        default=10_000,
        ge=1,
        description=(
            "statement_timeout set on every MCP read-only connection: a curated query "
            "cannot run away despite the unbounded tool output."
        ),
    )
    mcp_max_rows: int = Field(
        default=500,
        ge=1,
        description=(
            "Safety LIMIT injected into every MCP tool query: unbounded output can make "
            "the tool call itself fail (context overflow)."
        ),
    )
    mcp_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="TCP port the MCP server (uvicorn) listens on.",
    )


class Settings(McpSettings):
    """Application parameters, loaded from environment (or .env file).

    Prefer construction via get_settings(), which suppresses value leaks in
    pydantic error messages.
    """

    # PostgreSQL. SecretStr because DSN contains database password in cleartext.
    # Callers use .get_secret_value().
    postgres_dsn: SecretStr = Field(
        description="PostgreSQL connection DSN (e.g. postgresql://user:pass@host:5432/db).",
    )

    # Persisted Garmin session. Unprefixed name: garminconnect 0.3.2 reads
    # os.getenv("GARMINTOKENS") directly, no default path.
    garmin_tokens_path: Path = Field(
        validation_alias="GARMINTOKENS",
        description="Directory where Garmin session tokens are persisted.",
    )

    fit_archive_path: Path = Field(
        description="Directory (PVC) where original .FIT files are archived.",
    )

    garmin_username: SecretStr = Field(
        description="Garmin Connect account identifier (email).",
    )
    garmin_password: SecretStr = Field(
        description="Garmin Connect account password.",
    )

    # Scheduler (batch L11). Default matches
    # t0.ops.backfill.DEFAULT_HISTORY_START_ISO ("2025-04-03"): both
    # name the same history start, one for read-only ops tools, one for the
    # scheduler entry point. Duplicated, not imported: settings is loaded at
    # first startup before any DB connection; importing from ops would create
    # a dependency at that point.
    history_start: date = Field(
        default=date(2025, 4, 3),
        description="Start date for historical catchup of scheduled poll.",
    )
    refresh_lookback_days: int = Field(
        default=3,
        ge=0,
        description=(
            "Number of days systematically re-polled even if already 'done', "
            "to catch Garmin's late republication."
        ),
    )


def _env_var_name(field_name: str) -> str:
    """Environment variable name for a field."""
    field = McpSettings.model_fields.get(field_name)
    alias = getattr(field, "validation_alias", None) if field is not None else None
    if isinstance(alias, str):
        return alias
    return f"{ENV_PREFIX}{field_name.upper()}"


def _load(settings_cls: type[_SettingsT]) -> _SettingsT:
    """Build a settings object, converting ValidationError to SettingsError.

    Retains only loc and type; msg and input may contain the bad value.
    """
    try:
        return settings_cls()  # required fields come from the environment
    except ValidationError as exc:
        causes = ", ".join(
            f"{_env_var_name(str(error['loc'][0]) if error['loc'] else '?')} [{error['type']}]"
            for error in exc.errors()
        )
        raise SettingsError(
            "Invalid configuration, service refuses to start. "
            f"Offending variables: {causes}. "
            "Values intentionally omitted from this message."
        ) from None


def get_settings() -> Settings:
    """Build the full application settings from current environment.

    Function, not global singleton: each caller (including tests) explicitly
    controls when environment is read.

    Raises:
        SettingsError: if a variable is missing or invalid. Message cites only
            variable names--never values--so a failed startup does not write a
            Garmin password to container logs.
    """
    return _load(Settings)


def get_mcp_settings() -> McpSettings:
    """Build the MCP-server settings from current environment.

    Validates only the T0_MCP_* variables: the MCP pod holds no ingest
    credentials by design, so the ingest-only required fields must not be
    demanded of it.

    Raises:
        SettingsError: same contract as get_settings().
    """
    return _load(McpSettings)
