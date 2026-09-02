"""Tests for the settings module.

No network, no Garmin calls: local validation only.

Core concern: no identifier value must appear in repr, serialization, or exception
message, as these three paths end up in container logs.
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path

import pytest

import t0
from t0.settings import Settings, SettingsError, get_mcp_settings, get_settings

FAKE_TOKENS_DIR = "/opt/t0-test/garmin-tokens"
FAKE_FIT_ARCHIVE_DIR = "/opt/t0-test/fit-archive"
FAKE_USERNAME = "athlete@example.invalid"
FAKE_PASSWORD = "canari-mot-de-passe-factice"
FAKE_DSN_PASSWORD = "canari-mot-de-passe-base"
FAKE_DSN = f"postgresql://user:{FAKE_DSN_PASSWORD}@localhost:5432/db"
MCP_TOKEN_CANARY = "canari-jeton-mcp"
MCP_DSN_CANARY = "postgresql://t0_mcp_ro:canari-dsn-mcp@localhost:5432/db"

CANARIES = (FAKE_PASSWORD, FAKE_DSN_PASSWORD, FAKE_USERNAME)
INGEST_VARIABLES = (
    "T0_POSTGRES_DSN",
    "GARMINTOKENS",
    "T0_FIT_ARCHIVE_PATH",
    "T0_GARMIN_USERNAME",
    "T0_GARMIN_PASSWORD",
)


def _set_full_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T0_POSTGRES_DSN", FAKE_DSN)
    monkeypatch.setenv("GARMINTOKENS", FAKE_TOKENS_DIR)
    monkeypatch.setenv("T0_FIT_ARCHIVE_PATH", FAKE_FIT_ARCHIVE_DIR)
    monkeypatch.setenv("T0_GARMIN_USERNAME", FAKE_USERNAME)
    monkeypatch.setenv("T0_GARMIN_PASSWORD", FAKE_PASSWORD)


def test_package_exposes_a_version() -> None:
    assert t0.__version__


def test_settings_load_from_fake_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_full_environment(monkeypatch)

    settings = get_settings()

    assert settings.postgres_dsn.get_secret_value() == FAKE_DSN
    assert settings.garmin_tokens_path == Path(FAKE_TOKENS_DIR)
    assert settings.fit_archive_path == Path(FAKE_FIT_ARCHIVE_DIR)
    assert settings.garmin_username.get_secret_value() == FAKE_USERNAME
    assert settings.garmin_password.get_secret_value() == FAKE_PASSWORD


def test_garmintokens_is_read_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """``garminconnect`` reads ``GARMINTOKENS`` as is: the prefix must not apply."""
    _set_full_environment(monkeypatch)
    monkeypatch.delenv("GARMINTOKENS")
    monkeypatch.setenv("T0_GARMIN_TOKENS_PATH", FAKE_TOKENS_DIR)

    with pytest.raises(SettingsError):
        get_settings()


@pytest.mark.parametrize(
    "missing",
    [
        "T0_POSTGRES_DSN",
        "GARMINTOKENS",
        "T0_FIT_ARCHIVE_PATH",
        "T0_GARMIN_USERNAME",
        "T0_GARMIN_PASSWORD",
    ],
)
def test_missing_variable_is_fatal(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """No required field has an implicit default value."""
    _set_full_environment(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    # Word-boundary match: "GARMINTOKENS" must not pass via "T0_GARMINTOKENS".
    assert re.search(rf"\b{re.escape(missing)} \[", str(excinfo.value))


@pytest.mark.parametrize(
    "missing",
    [
        "T0_POSTGRES_DSN",
        "T0_FIT_ARCHIVE_PATH",
        "T0_GARMIN_USERNAME",
    ],
)
def test_validation_error_never_reveals_a_secret(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Regression: pydantic copies the input dictionary back (password included).

    ``get_settings`` must intercept that, including in the full traceback -- a chained
    exception would reintroduce the leak through ``__cause__`` / ``__context__``.
    """
    _set_full_environment(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    for canary in CANARIES:
        assert canary not in rendered
        assert canary not in str(excinfo.value)


def test_secrets_never_appear_in_repr_or_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """``repr``/``str``/``model_dump`` of a settings object reveal nothing."""
    _set_full_environment(monkeypatch)

    settings = get_settings()
    renderings = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        f"{settings}",
        str(settings.postgres_dsn),
        str(settings.garmin_password),
    ]

    for rendering in renderings:
        for canary in CANARIES:
            assert canary not in rendering, f"leak of {canary!r} in {rendering!r}"


def test_settings_can_be_built_without_reading_a_local_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.env`` present in the current directory must not pollute the tests."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("T0_GARMIN_PASSWORD=value-from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _set_full_environment(monkeypatch)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.garmin_password.get_secret_value() == FAKE_PASSWORD


def test_mcp_fields_are_optional_with_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest and the scheduler share this Settings class and their environments
    carry no MCP variable: every MCP field must default to None or a safe value,
    never to a credential."""
    _set_full_environment(monkeypatch)

    settings = get_settings()

    assert settings.mcp_bearer_token is None
    assert settings.mcp_postgres_dsn is None
    assert settings.mcp_statement_timeout_ms == 10_000
    assert settings.mcp_max_rows == 500
    assert settings.mcp_port == 8000
    assert settings.mcp_metrics_port == 9090


def test_mcp_secrets_never_appear_in_repr_or_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract as the Garmin/DSN secrets: the MCP token and DSN live in
    K8s Secrets and must not leak through any rendering path."""
    mcp_token = "canari-jeton-mcp"
    mcp_dsn = "postgresql://t0_mcp_ro:canari-dsn-mcp@localhost:5432/db"
    monkeypatch.setenv("T0_MCP_BEARER_TOKEN", mcp_token)
    monkeypatch.setenv("T0_MCP_POSTGRES_DSN", mcp_dsn)
    _set_full_environment(monkeypatch)

    settings = get_settings()
    renderings = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        str(settings.mcp_bearer_token),
        str(settings.mcp_postgres_dsn),
    ]

    for rendering in renderings:
        assert "canari-jeton-mcp" not in rendering
        assert "canari-dsn-mcp" not in rendering


def test_get_mcp_settings_loads_without_ingest_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP pod carries no ingest credential: validation must not demand one."""
    for variable in INGEST_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("T0_MCP_BEARER_TOKEN", MCP_TOKEN_CANARY)
    monkeypatch.setenv("T0_MCP_POSTGRES_DSN", MCP_DSN_CANARY)

    settings = get_mcp_settings()

    assert settings.mcp_bearer_token is not None
    assert settings.mcp_postgres_dsn is not None
    assert settings.mcp_bearer_token.get_secret_value() == MCP_TOKEN_CANARY
    assert settings.mcp_postgres_dsn.get_secret_value() == MCP_DSN_CANARY
    assert settings.mcp_port == 8000


def test_get_mcp_settings_ignores_ingest_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra="ignore": an MCP process run where ingest variables exist stays MCP-scoped."""
    _set_full_environment(monkeypatch)

    settings = get_mcp_settings()

    assert settings.mcp_bearer_token is None
    assert not hasattr(settings, "postgres_dsn")


def test_mcp_validation_error_never_reveals_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same leak contract as get_settings, exercised on the MCP scope."""
    monkeypatch.setenv("T0_MCP_BEARER_TOKEN", MCP_TOKEN_CANARY)
    monkeypatch.setenv("T0_MCP_POSTGRES_DSN", MCP_DSN_CANARY)
    monkeypatch.setenv("T0_MCP_PORT", "not-a-port")

    with pytest.raises(SettingsError) as excinfo:
        get_mcp_settings()

    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    for canary in (MCP_TOKEN_CANARY, MCP_DSN_CANARY):
        assert canary not in rendered
        assert canary not in str(excinfo.value)
