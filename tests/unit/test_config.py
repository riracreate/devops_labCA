"""Configuration parsing and validation."""

import pytest

from catalog.config import ENV_VARS, ConfigError, Settings


def test_defaults_are_production_safe():
    settings = Settings.from_env({})
    assert settings.db_timeout_seconds == 1.0
    assert settings.redis_timeout_seconds == 0.25
    assert settings.breaker_failure_threshold == 3


def test_values_are_parsed_by_type():
    settings = Settings.from_env(
        {
            "DB_PORT": "15432",
            "DB_TIMEOUT_SECONDS": "0.5",
            "SEED_DATA": "false",
            "APP_VERSION": "abc123",
            "CB_RESET_TIMEOUT_SECONDS": " 2 ",
        }
    )
    assert settings.db_port == 15432
    assert settings.db_timeout_seconds == 0.5
    assert settings.seed_data is False
    assert settings.version == "abc123"
    assert settings.breaker_reset_timeout_seconds == 2.0


def test_empty_values_fall_back_to_defaults():
    assert Settings.from_env({"DB_HOST": "  "}).db_host == "localhost"


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("DB_PORT", "not-a-number"),
        ("SEED_DATA", "maybe"),
        ("DB_TIMEOUT_SECONDS", "0"),
        ("REDIS_TIMEOUT_SECONDS", "-1"),
        ("DB_MAX_RETRIES", "-1"),
        ("DB_POOL_SIZE", "0"),
        ("CB_FAILURE_THRESHOLD", "0"),
        ("CB_RESET_TIMEOUT_SECONDS", "0"),
        ("CACHE_FRESH_SECONDS", "-5"),
        ("CACHE_STALE_SECONDS", "1"),  # shorter than the 30 s fresh window
    ],
)
def test_invalid_values_are_rejected_with_the_variable_name(var, value):
    with pytest.raises(ConfigError, match=var):
        Settings.from_env({var: value})


def test_every_setting_has_an_environment_variable():
    from dataclasses import fields

    assert {f.name for f in fields(Settings)} == set(ENV_VARS)


def test_public_view_hides_the_password():
    view = Settings(db_password="hunter2").public_view()
    assert "db_password" not in view
    assert "hunter2" not in str(view)
