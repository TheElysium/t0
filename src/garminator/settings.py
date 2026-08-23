"""garminator configuration, read from environment.

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
from pathlib import Path  # noqa: TC003 — pydantic doit résoudre le type à l'exécution

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "GARMINATOR_"


class SettingsError(RuntimeError):
    """Invalid configuration.

    Never contains environment values: only variable names and error codes.
    """


class Settings(BaseSettings):
    """Application parameters, loaded from environment (or .env file).

    Prefer construction via get_settings(), which suppresses value leaks in
    pydantic error messages.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL. SecretStr because DSN contains database password in cleartext.
    # Callers use .get_secret_value().
    postgres_dsn: SecretStr = Field(
        description="DSN de connexion PostgreSQL (ex. postgresql://user:pass@host:5432/db).",
    )

    # Persisted Garmin session. Unprefixed name: garminconnect 0.3.2 reads
    # os.getenv("GARMINTOKENS") directly, no default path.
    garmin_tokens_path: Path = Field(
        validation_alias="GARMINTOKENS",
        description="Répertoire où sont persistés les jetons de session Garmin.",
    )

    fit_archive_path: Path = Field(
        description="Répertoire (PVC) où sont archivés les fichiers .FIT originaux.",
    )

    garmin_username: SecretStr = Field(
        description="Identifiant (e-mail) du compte Garmin Connect.",
    )
    garmin_password: SecretStr = Field(
        description="Mot de passe du compte Garmin Connect.",
    )

    # Scheduler (batch L11). Default matches
    # garminator.ops.backfill.DEFAULT_HISTORY_START_ISO ("2025-04-03"): both
    # name the same history start, one for read-only ops tools, one for the
    # scheduler entry point. Duplicated, not imported: settings is loaded at
    # first startup before any DB connection; importing from ops would create
    # a dependency at that point.
    history_start: date = Field(
        default=date(2025, 4, 3),
        description="Date de début du rattrapage historique du sondage planifié.",
    )
    refresh_lookback_days: int = Field(
        default=3,
        ge=0,
        description=(
            "Nombre de jours resondés systématiquement même si déjà 'done', pour "
            "rattraper une republication tardive de Garmin."
        ),
    )


def _env_var_name(field_name: str) -> str:
    """Environment variable name for a field."""
    field = Settings.model_fields.get(field_name)
    alias = getattr(field, "validation_alias", None) if field is not None else None
    if isinstance(alias, str):
        return alias
    return f"{ENV_PREFIX}{field_name.upper()}"


def get_settings() -> Settings:
    """Build settings from current environment.

    Function, not global singleton: each caller (including tests) explicitly
    controls when environment is read.

    Raises:
        SettingsError: if a variable is missing or invalid. Message cites only
            variable names—never values—so a failed startup does not write a
            Garmin password to container logs.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # required fields come from env
    except ValidationError as exc:
        # Retain only loc and type; msg and input may contain the bad value.
        causes = ", ".join(
            f"{_env_var_name(str(error['loc'][0]) if error['loc'] else '?')} [{error['type']}]"
            for error in exc.errors()
        )
        raise SettingsError(
            "Configuration invalide, le service refuse de démarrer. "
            f"Variables en cause : {causes}. "
            "Les valeurs sont volontairement omises de ce message."
        ) from None
