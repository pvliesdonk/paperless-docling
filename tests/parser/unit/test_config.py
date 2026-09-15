import os
import traceback
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest
from paperless_docling.config import PluginConfig
from paperless_docling.errors import ConfigurationError


def valid_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    environment = {
        "PAPERLESS_DOCLING_SERVE_URL": "https://docling.example.test",
        "PAPERLESS_DOCLING_PROFILE_VERSION": "profile-v1",
        "PAPERLESS_DOCLING_CACHE_DIR": str(tmp_path / "cache"),
    }
    environment.update(overrides)
    return environment


@pytest.mark.parametrize(
    "variable",
    [
        "PAPERLESS_DOCLING_SERVE_URL",
        "PAPERLESS_DOCLING_PROFILE_VERSION",
        "PAPERLESS_DOCLING_CACHE_DIR",
    ],
)
def test_required_configuration_rejects_missing_or_empty_values(tmp_path, variable):
    for absent_value in (None, ""):
        environment = valid_env(tmp_path)
        if absent_value is None:
            environment.pop(variable)
        else:
            environment[variable] = absent_value

        with pytest.raises(ConfigurationError, match=variable):
            PluginConfig.from_environment(environment)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://docling.example.test",
        "docling.example.test",
        "https:///missing-host",
    ],
)
def test_serve_url_rejects_non_http_or_incomplete_urls(tmp_path, url):
    environment = valid_env(tmp_path, PAPERLESS_DOCLING_SERVE_URL=url)

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_SERVE_URL"):
        PluginConfig.from_environment(environment)


@pytest.mark.parametrize(
    "url",
    [
        "https://:443",
        "https://[highly-secret",
        "https://docling.example.test:invalid",
    ],
)
def test_serve_url_normalizes_invalid_authority_errors_without_leaking_values(
    tmp_path,
    url,
):
    environment = valid_env(tmp_path, PAPERLESS_DOCLING_SERVE_URL=url)

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_SERVE_URL") as error:
        PluginConfig.from_environment(environment)

    assert url not in str(error.value)
    assert "highly-secret" not in str(error.value)


def test_serve_url_parser_error_traceback_does_not_leak_supplied_value(tmp_path):
    secret = "highly-secret-port"
    environment = valid_env(
        tmp_path,
        PAPERLESS_DOCLING_SERVE_URL=f"https://docling.example.test:{secret}",
    )

    with pytest.raises(ConfigurationError) as error:
        PluginConfig.from_environment(environment)

    formatted_traceback = "".join(
        traceback.format_exception(error.type, error.value, error.tb),
    )
    assert secret not in formatted_traceback
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_serve_url_rejects_userinfo_without_leaking_credentials(tmp_path):
    username = "highly-secret-user"
    password = "highly-secret-password"
    environment = valid_env(
        tmp_path,
        PAPERLESS_DOCLING_SERVE_URL=(
            f"https://{username}:{password}@docling.example.test"
        ),
    )

    with pytest.raises(ConfigurationError) as error:
        PluginConfig.from_environment(environment)

    diagnostics = "".join(
        [
            repr(error.value),
            *traceback.format_exception(error.type, error.value, error.tb),
        ],
    )
    assert username not in diagnostics
    assert password not in diagnostics


@pytest.mark.parametrize(
    "url",
    ["http://docling.example.test", "https://docling.example.test/api"],
)
def test_serve_url_accepts_http_and_https_urls(tmp_path, url):
    config = PluginConfig.from_environment(
        valid_env(tmp_path, PAPERLESS_DOCLING_SERVE_URL=url),
    )

    assert config.serve_url == url


def test_defaults_are_conservative_operational_values(tmp_path):
    config = PluginConfig.from_environment(valid_env(tmp_path))

    assert config.token_file is None
    assert config.token is None
    assert config.preset == "paperless-vlm"
    assert config.poll_interval == timedelta(seconds=2)
    assert config.deadline == timedelta(minutes=10)
    assert config.cache_max_age == timedelta(days=30)
    assert config.cache_max_bytes == 1_073_741_824
    assert config.allow_unsupported_paperless is False


def test_configuration_is_immutable(tmp_path):
    config = PluginConfig.from_environment(valid_env(tmp_path))

    with pytest.raises(FrozenInstanceError):
        config.preset = "changed"


def test_token_is_read_once_and_one_trailing_newline_is_removed(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret-value\n\n")
    config = PluginConfig.from_environment(
        valid_env(
            tmp_path,
            PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(token_file),
        ),
    )

    token_file.unlink()

    assert config.token_file == token_file
    assert config.token == "secret-value\n"


@pytest.mark.parametrize("token_path_kind", ["missing", "directory"])
def test_token_file_must_be_a_readable_regular_file(tmp_path, token_path_kind):
    token_path = tmp_path / "token"
    if token_path_kind == "directory":
        token_path.mkdir()
    environment = valid_env(
        tmp_path,
        PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(token_path),
    )

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_SERVE_TOKEN_FILE"):
        PluginConfig.from_environment(environment)


def test_token_read_permission_error_is_translated_without_leaking_details(
    tmp_path,
    monkeypatch,
):
    token_file = tmp_path / "token"
    token_file.write_text("token-on-disk")
    read_error = PermissionError("highly-secret diagnostic")

    def fail_read(path):
        raise read_error

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(ConfigurationError) as error:
        PluginConfig.from_environment(
            valid_env(
                tmp_path,
                PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(token_file),
            ),
        )

    formatted_traceback = "".join(
        traceback.format_exception(error.type, error.value, error.tb),
    )
    assert "highly-secret" not in formatted_traceback
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_token_is_excluded_from_representation_and_equality(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("first-secret")

    first = PluginConfig.from_environment(
        valid_env(
            tmp_path,
            PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(token_file),
        ),
    )
    token_file.write_text("second-secret")
    second = PluginConfig.from_environment(
        valid_env(
            tmp_path,
            PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(token_file),
        ),
    )

    assert first == second
    assert "first-secret" not in repr(first)
    assert "second-secret" not in repr(second)


@pytest.mark.parametrize(
    ("variable", "accepted", "rejected"),
    [
        ("PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS", "60", "60.000001"),
        ("PAPERLESS_DOCLING_DEADLINE_SECONDS", "86400", "86400.000001"),
        ("PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS", "365", "365.000001"),
        ("PAPERLESS_DOCLING_CACHE_MAX_BYTES", "1099511627776", "1099511627777"),
    ],
)
def test_numeric_upper_bound_is_inclusive(tmp_path, variable, accepted, rejected):
    PluginConfig.from_environment(valid_env(tmp_path, **{variable: accepted}))

    with pytest.raises(ConfigurationError, match=variable):
        PluginConfig.from_environment(valid_env(tmp_path, **{variable: rejected}))


@pytest.mark.parametrize(
    "variable",
    [
        "PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS",
        "PAPERLESS_DOCLING_DEADLINE_SECONDS",
        "PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS",
        "PAPERLESS_DOCLING_CACHE_MAX_BYTES",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_numeric_configuration_requires_positive_numbers(tmp_path, variable, value):
    with pytest.raises(ConfigurationError, match=variable):
        PluginConfig.from_environment(valid_env(tmp_path, **{variable: value}))


@pytest.mark.parametrize(
    "variable",
    [
        "PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS",
        "PAPERLESS_DOCLING_DEADLINE_SECONDS",
        "PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS",
    ],
)
def test_duration_rejects_positive_values_below_timedelta_resolution(
    tmp_path,
    variable,
):
    with pytest.raises(ConfigurationError, match=variable):
        PluginConfig.from_environment(
            valid_env(tmp_path, **{variable: "0.000000000001"}),
        )


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        (
            "PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS",
            "0.000001",
            "poll_interval",
            timedelta(microseconds=1),
        ),
        (
            "PAPERLESS_DOCLING_DEADLINE_SECONDS",
            "0.000001",
            "deadline",
            timedelta(microseconds=1),
        ),
        (
            "PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS",
            "0.000000000011574074074074074074",
            "cache_max_age",
            timedelta(microseconds=1),
        ),
        (
            "PAPERLESS_DOCLING_CACHE_MAX_BYTES",
            "1",
            "cache_max_bytes",
            1,
        ),
    ],
)
def test_numeric_lower_bound_is_inclusive(
    tmp_path,
    variable,
    value,
    field,
    expected,
):
    config = PluginConfig.from_environment(
        valid_env(tmp_path, **{variable: value}),
    )

    assert getattr(config, field) == expected


def test_explicit_numeric_configuration_is_parsed_to_domain_types(tmp_path):
    config = PluginConfig.from_environment(
        valid_env(
            tmp_path,
            PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS="0.25",
            PAPERLESS_DOCLING_DEADLINE_SECONDS="90.5",
            PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS="7.5",
            PAPERLESS_DOCLING_CACHE_MAX_BYTES="2048",
        ),
    )

    assert config.poll_interval == timedelta(milliseconds=250)
    assert config.deadline == timedelta(seconds=90, milliseconds=500)
    assert config.cache_max_age == timedelta(days=7, hours=12)
    assert config.cache_max_bytes == 2048


def test_cache_max_bytes_rejects_fractional_values(tmp_path):
    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_MAX_BYTES"):
        PluginConfig.from_environment(
            valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_MAX_BYTES="1.5"),
        )


def test_cache_directory_must_be_absolute(tmp_path):
    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_DIR"):
        PluginConfig.from_environment(
            valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_DIR="relative/cache"),
        )


def test_cache_directory_accepts_a_writable_absolute_path(tmp_path):
    cache_dir = tmp_path / "not-created-yet" / "cache"

    config = PluginConfig.from_environment(
        valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_DIR=str(cache_dir)),
    )

    assert config.cache_dir == cache_dir
    assert not cache_dir.exists()


def test_cache_directory_rejects_a_regular_file(tmp_path):
    cache_file = tmp_path / "cache"
    cache_file.write_text("not a directory")

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_DIR"):
        PluginConfig.from_environment(
            valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_DIR=str(cache_file)),
        )


def test_cache_directory_rejects_a_non_writable_location(tmp_path):
    cache_parent = tmp_path / "read-only"
    cache_parent.mkdir(mode=0o500)

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_DIR"):
        PluginConfig.from_environment(
            valid_env(
                tmp_path,
                PAPERLESS_DOCLING_CACHE_DIR=str(cache_parent / "cache"),
            ),
        )


def test_cache_directory_uses_effective_process_access(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    supports_effective_ids = os.access in os.supports_effective_ids

    def deny_effective_access(path, mode, *, effective_ids=False):
        return not (
            path == tmp_path
            and mode == os.W_OK | os.X_OK
            and effective_ids is supports_effective_ids
        )

    monkeypatch.setattr(os, "access", deny_effective_access)

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_DIR"):
        PluginConfig.from_environment(
            valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_DIR=str(cache_dir)),
        )

    assert not cache_dir.exists()


def test_missing_cache_directory_rejects_existing_regular_file_ancestor(tmp_path):
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("content")
    regular_file.chmod(0o700)
    cache_dir = regular_file / "cache"

    with pytest.raises(ConfigurationError, match="PAPERLESS_DOCLING_CACHE_DIR"):
        PluginConfig.from_environment(
            valid_env(tmp_path, PAPERLESS_DOCLING_CACHE_DIR=str(cache_dir)),
        )

    assert not cache_dir.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False)],
)
def test_compatibility_override_accepts_only_canonical_booleans(
    tmp_path,
    value,
    expected,
):
    config = PluginConfig.from_environment(
        valid_env(
            tmp_path,
            PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS=value,
        ),
    )

    assert config.allow_unsupported_paperless is expected


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE"])
def test_compatibility_override_rejects_ambiguous_booleans(tmp_path, value):
    with pytest.raises(ConfigurationError, match="must be either 'true' or 'false'"):
        PluginConfig.from_environment(
            valid_env(
                tmp_path,
                PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS=value,
            ),
        )


def test_error_never_contains_token_value(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("highly-secret")
    environment = valid_env(
        tmp_path,
        PAPERLESS_DOCLING_SERVE_TOKEN_FILE=str(secret),
        PAPERLESS_DOCLING_DEADLINE_SECONDS="invalid",
    )

    with pytest.raises(ConfigurationError) as error:
        PluginConfig.from_environment(environment)

    assert "highly-secret" not in str(error.value)
