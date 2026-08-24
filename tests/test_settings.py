"""Tests for the settings module.

No network, no Garmin calls: local validation only.

Core concern: no identifier value must appear in repr, serialization, or exception
message, as these three paths end up in container logs.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

import garminator
from garminator.settings import Settings, SettingsError, get_settings

FAKE_TOKENS_DIR = "/opt/garminator-test/garmin-tokens"
FAKE_FIT_ARCHIVE_DIR = "/opt/garminator-test/fit-archive"
FAKE_USERNAME = "athlete@example.invalid"
FAKE_PASSWORD = "canari-mot-de-passe-factice"
FAKE_DSN_PASSWORD = "canari-mot-de-passe-base"
FAKE_DSN = f"postgresql://user:{FAKE_DSN_PASSWORD}@localhost:5432/db"

CANARIES = (FAKE_PASSWORD, FAKE_DSN_PASSWORD, FAKE_USERNAME)


def _set_full_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMINATOR_POSTGRES_DSN", FAKE_DSN)
    monkeypatch.setenv("GARMINTOKENS", FAKE_TOKENS_DIR)
    monkeypatch.setenv("GARMINATOR_FIT_ARCHIVE_PATH", FAKE_FIT_ARCHIVE_DIR)
    monkeypatch.setenv("GARMINATOR_GARMIN_USERNAME", FAKE_USERNAME)
    monkeypatch.setenv("GARMINATOR_GARMIN_PASSWORD", FAKE_PASSWORD)


def test_package_exposes_a_version() -> None:
    assert garminator.__version__


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
    monkeypatch.setenv("GARMINATOR_GARMIN_TOKENS_PATH", FAKE_TOKENS_DIR)

    with pytest.raises(SettingsError):
        get_settings()


@pytest.mark.parametrize(
    "missing",
    [
        "GARMINATOR_POSTGRES_DSN",
        "GARMINTOKENS",
        "GARMINATOR_FIT_ARCHIVE_PATH",
        "GARMINATOR_GARMIN_USERNAME",
        "GARMINATOR_GARMIN_PASSWORD",
    ],
)
def test_missing_variable_is_fatal(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """No required field has an implicit default value."""
    _set_full_environment(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert missing in str(excinfo.value)


@pytest.mark.parametrize(
    "missing",
    [
        "GARMINATOR_POSTGRES_DSN",
        "GARMINATOR_FIT_ARCHIVE_PATH",
        "GARMINATOR_GARMIN_USERNAME",
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
    dotenv.write_text("GARMINATOR_GARMIN_PASSWORD=value-from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _set_full_environment(monkeypatch)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.garmin_password.get_secret_value() == FAKE_PASSWORD
