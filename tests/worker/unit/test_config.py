import os
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from paperless_docling_worker.config import ConfigurationError, WorkerConfig


def valid_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    paperless_token = tmp_path / "paperless-token"
    webhook_token = tmp_path / "webhook-token"
    llm_api_key = tmp_path / "llm-api-key"
    paperless_token.write_text("paperless-secret\n")
    webhook_token.write_text("webhook-secret\n")
    llm_api_key.write_text("llm-secret\n")
    environment = {
        "PAPERLESS_URL": "https://paperless.example.test",
        "PAPERLESS_TOKEN_FILE": str(paperless_token),
        "WEBHOOK_TOKEN_FILE": str(webhook_token),
        "SUMMARY_DATABASE_PATH": str(tmp_path / "state" / "summary.sqlite3"),
        "SUMMARY_LLM_BASE_URL": "https://llm.example.test/v1",
        "SUMMARY_LLM_API_KEY_FILE": str(llm_api_key),
        "SUMMARY_LLM_MODEL": "summary-model",
        "SUMMARY_PROFILE_VERSION": "profile-v1",
    }
    environment.update(overrides)
    return environment


@pytest.mark.parametrize(
    "variable",
    [
        "PAPERLESS_URL",
        "PAPERLESS_TOKEN_FILE",
        "WEBHOOK_TOKEN_FILE",
        "SUMMARY_DATABASE_PATH",
        "SUMMARY_LLM_BASE_URL",
        "SUMMARY_LLM_API_KEY_FILE",
        "SUMMARY_LLM_MODEL",
        "SUMMARY_PROFILE_VERSION",
    ],
)
def test_required_configuration_names_missing_and_empty_values(tmp_path, variable):
    for absent_value in (None, ""):
        environment = valid_env(tmp_path)
        if absent_value is None:
            environment.pop(variable)
        else:
            environment[variable] = absent_value

        with pytest.raises(ConfigurationError, match=variable):
            WorkerConfig.from_environment(environment)


@pytest.mark.parametrize("variable", ["PAPERLESS_URL", "SUMMARY_LLM_BASE_URL"])
@pytest.mark.parametrize(
    "url",
    ["ftp://service.example.test", "service.example.test", "https:///missing-host"],
)
def test_service_urls_require_complete_http_urls(tmp_path, variable, url):
    with pytest.raises(ConfigurationError, match=variable):
        WorkerConfig.from_environment(valid_env(tmp_path, **{variable: url}))


def test_secret_files_are_read_once_and_redacted_from_identity(tmp_path):
    environment = valid_env(tmp_path)
    first = WorkerConfig.from_environment(environment)
    Path(environment["PAPERLESS_TOKEN_FILE"]).write_text("replacement")
    second = WorkerConfig.from_environment(environment)

    Path(environment["PAPERLESS_TOKEN_FILE"]).unlink()

    assert first.paperless_token.get_secret_value() == "paperless-secret"
    assert first.webhook_token.get_secret_value() == "webhook-secret"
    assert first.llm_api_key.get_secret_value() == "llm-secret"
    assert first == second
    representation = repr(first)
    assert "paperless-secret" not in representation
    assert "webhook-secret" not in representation
    assert "llm-secret" not in representation


@pytest.mark.parametrize(
    ("variable", "attribute"),
    [
        ("PAPERLESS_TOKEN_FILE", "paperless_token"),
        ("WEBHOOK_TOKEN_FILE", "webhook_token"),
        ("SUMMARY_LLM_API_KEY_FILE", "llm_api_key"),
    ],
)
def test_secret_files_normalize_one_trailing_crlf(tmp_path, variable, attribute):
    environment = valid_env(tmp_path)
    Path(environment[variable]).write_bytes(b"credential-secret\r\n")

    config = WorkerConfig.from_environment(environment)

    assert getattr(config, attribute).get_secret_value() == "credential-secret"


@pytest.mark.parametrize(
    ("token", "disclosure_marker"),
    [
        ("\n", None),
        ("\r\n", None),
        ("private-credential\n\n", "private-credential"),
        ("private-credential\r\n\r\n", "private-credential"),
        ("private-credential\r", "private-credential"),
        ("private credential", "private credential"),
        ("private\tcredential", "private\tcredential"),
        ("private\ncredential", "private\ncredential"),
    ],
    ids=[
        "empty-lf",
        "empty-crlf",
        "repeated-lf",
        "repeated-crlf",
        "lone-cr",
        "space",
        "tab",
        "newline",
    ],
)
def test_webhook_token_rejects_credentials_authentication_cannot_accept(
    tmp_path, token, disclosure_marker
):
    environment = valid_env(tmp_path)
    Path(environment["WEBHOOK_TOKEN_FILE"]).write_text(token)

    with pytest.raises(ConfigurationError, match="WEBHOOK_TOKEN_FILE") as error:
        WorkerConfig.from_environment(environment)

    diagnostics = "".join(
        [repr(error.value), *traceback.format_exception(error.type, error.value, error.tb)]
    )
    if disclosure_marker is not None:
        assert disclosure_marker not in diagnostics


@pytest.mark.parametrize("token", ["abc-XYZ_123.~+/=", "punctuation:!@#$%^&*()"])
def test_webhook_token_accepts_non_whitespace_credential_characters(tmp_path, token):
    environment = valid_env(tmp_path)
    Path(environment["WEBHOOK_TOKEN_FILE"]).write_text(token + "\n")

    config = WorkerConfig.from_environment(environment)

    assert config.webhook_token.get_secret_value() == token


@pytest.mark.parametrize(
    ("variable", "attribute", "initial", "replacement"),
    [
        (
            "PAPERLESS_TOKEN_FILE",
            "paperless_token",
            "paperless-secret",
            "replacement-paperless-secret",
        ),
        (
            "WEBHOOK_TOKEN_FILE",
            "webhook_token",
            "webhook-secret",
            "replacement-webhook-secret",
        ),
        (
            "SUMMARY_LLM_API_KEY_FILE",
            "llm_api_key",
            "llm-secret",
            "replacement-llm-secret",
        ),
    ],
)
def test_each_secret_is_independently_excluded_from_equality_and_repr(
    tmp_path,
    variable,
    attribute,
    initial,
    replacement,
):
    environment = valid_env(tmp_path)
    first = WorkerConfig.from_environment(environment)
    Path(environment[variable]).write_text(replacement)
    second = WorkerConfig.from_environment(environment)

    assert getattr(first, attribute).get_secret_value() == initial
    assert getattr(second, attribute).get_secret_value() == replacement
    assert first == second
    assert initial not in repr(first)
    assert replacement not in repr(second)


@pytest.mark.parametrize(
    "variable",
    ["PAPERLESS_TOKEN_FILE", "WEBHOOK_TOKEN_FILE", "SUMMARY_LLM_API_KEY_FILE"],
)
def test_secret_file_errors_do_not_disclose_file_contents(tmp_path, variable):
    secret_file = tmp_path / "secret"
    secret_file.write_text("highly-secret-value")
    environment = valid_env(tmp_path, **{variable: str(secret_file)})

    original_read_text = Path.read_text

    def fail_selected_file(path, *args, **kwargs):
        if path == secret_file:
            raise PermissionError("highly-secret-diagnostic")
        return original_read_text(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Path, "read_text", fail_selected_file)
        with pytest.raises(ConfigurationError, match=variable) as error:
            WorkerConfig.from_environment(environment)

    diagnostics = "".join(
        [repr(error.value), *traceback.format_exception(error.type, error.value, error.tb)]
    )
    assert "highly-secret-value" not in diagnostics
    assert "highly-secret-diagnostic" not in diagnostics
    assert error.value.__cause__ is None


@pytest.mark.parametrize("path_kind", ["missing", "directory", "empty"])
def test_secret_files_must_be_non_empty_readable_regular_files(tmp_path, path_kind):
    token_path = tmp_path / "invalid-token"
    if path_kind == "directory":
        token_path.mkdir()
    elif path_kind == "empty":
        token_path.write_text("")

    with pytest.raises(ConfigurationError, match="WEBHOOK_TOKEN_FILE"):
        WorkerConfig.from_environment(
            valid_env(tmp_path, WEBHOOK_TOKEN_FILE=str(token_path))
        )


def test_database_path_requires_a_writable_absolute_parent(tmp_path, monkeypatch):
    relative = valid_env(tmp_path, SUMMARY_DATABASE_PATH="state/summary.sqlite3")
    with pytest.raises(ConfigurationError, match="SUMMARY_DATABASE_PATH"):
        WorkerConfig.from_environment(relative)

    database_path = tmp_path / "state" / "summary.sqlite3"
    supports_effective_ids = os.access in os.supports_effective_ids

    def deny_parent(path, mode, *, effective_ids=False):
        return not (
            path == tmp_path
            and mode == os.W_OK | os.X_OK
            and effective_ids is supports_effective_ids
        )

    monkeypatch.setattr(os, "access", deny_parent)
    with pytest.raises(ConfigurationError, match="SUMMARY_DATABASE_PATH"):
        WorkerConfig.from_environment(
            valid_env(tmp_path, SUMMARY_DATABASE_PATH=str(database_path))
        )


def test_defaults_are_named_conservative_operational_values(tmp_path):
    config = WorkerConfig.from_environment(valid_env(tmp_path))

    assert config.context_tokens == 32_768
    assert config.output_tokens == 1_024
    assert config.chunk_tokens == 12_000
    assert config.max_attempts == 5
    assert config.lease_seconds == 300
    assert config.retry_base_seconds == 5
    assert config.retry_max_seconds == 300
    assert config.retry_jitter_seconds == 1
    assert config.max_request_bytes == 65_536
    assert config.concurrency == 1


@pytest.mark.parametrize(
    ("variable", "valid", "invalid", "companions"),
    [
        ("SUMMARY_CONTEXT_TOKENS", "1000000", "1000001", {}),
        (
            "SUMMARY_OUTPUT_TOKENS",
            "100000",
            "100001",
            {"SUMMARY_CONTEXT_TOKENS": "1000000"},
        ),
        (
            "SUMMARY_CHUNK_TOKENS",
            "900000",
            "900001",
            {
                "SUMMARY_CONTEXT_TOKENS": "1000000",
                "SUMMARY_OUTPUT_TOKENS": "99999",
            },
        ),
        ("SUMMARY_MAX_ATTEMPTS", "100", "101", {}),
        ("SUMMARY_LEASE_SECONDS", "3600", "3601", {}),
        (
            "SUMMARY_RETRY_BASE_SECONDS",
            "3600",
            "3601",
            {"SUMMARY_RETRY_MAX_SECONDS": "3600"},
        ),
        ("SUMMARY_RETRY_MAX_SECONDS", "86400", "86401", {}),
        (
            "SUMMARY_RETRY_JITTER_SECONDS",
            "3600",
            "3601",
            {"SUMMARY_RETRY_MAX_SECONDS": "3600"},
        ),
        ("SUMMARY_MAX_REQUEST_BYTES", "1048576", "1048577", {}),
    ],
)
def test_numeric_configuration_has_inclusive_upper_bounds(
    tmp_path, variable, valid, invalid, companions
):
    valid_values = {variable: valid, **companions}
    WorkerConfig.from_environment(valid_env(tmp_path, **valid_values))

    with pytest.raises(ConfigurationError, match=variable):
        invalid_values = {variable: invalid, **companions}
        WorkerConfig.from_environment(valid_env(tmp_path, **invalid_values))


@pytest.mark.parametrize(
    "variable",
    [
        "SUMMARY_CONTEXT_TOKENS",
        "SUMMARY_OUTPUT_TOKENS",
        "SUMMARY_CHUNK_TOKENS",
        "SUMMARY_MAX_ATTEMPTS",
        "SUMMARY_LEASE_SECONDS",
        "SUMMARY_RETRY_BASE_SECONDS",
        "SUMMARY_RETRY_MAX_SECONDS",
        "SUMMARY_RETRY_JITTER_SECONDS",
        "SUMMARY_MAX_REQUEST_BYTES",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid"])
def test_numeric_configuration_requires_positive_integers(tmp_path, variable, value):
    with pytest.raises(ConfigurationError, match=variable):
        WorkerConfig.from_environment(valid_env(tmp_path, **{variable: value}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"SUMMARY_CONTEXT_TOKENS": "4096", "SUMMARY_OUTPUT_TOKENS": "4096"},
        {
            "SUMMARY_CONTEXT_TOKENS": "4096",
            "SUMMARY_OUTPUT_TOKENS": "1024",
            "SUMMARY_CHUNK_TOKENS": "3072",
        },
    ],
)
def test_token_budget_reserves_output_space_beyond_each_chunk(tmp_path, overrides):
    with pytest.raises(
        ConfigurationError,
        match="SUMMARY_CHUNK_TOKENS.*SUMMARY_CONTEXT_TOKENS.*SUMMARY_OUTPUT_TOKENS",
    ):
        WorkerConfig.from_environment(valid_env(tmp_path, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"SUMMARY_RETRY_BASE_SECONDS": "10", "SUMMARY_RETRY_MAX_SECONDS": "9"},
        {"SUMMARY_RETRY_JITTER_SECONDS": "11", "SUMMARY_RETRY_MAX_SECONDS": "10"},
    ],
)
def test_retry_delays_and_jitter_are_capped(tmp_path, overrides):
    with pytest.raises(ConfigurationError, match="SUMMARY_RETRY_MAX_SECONDS"):
        WorkerConfig.from_environment(valid_env(tmp_path, **overrides))


@pytest.mark.parametrize("value", ["0", "2", "invalid"])
def test_summary_concurrency_is_fixed_to_one(tmp_path, value):
    with pytest.raises(ConfigurationError, match="SUMMARY_CONCURRENCY"):
        WorkerConfig.from_environment(
            valid_env(tmp_path, SUMMARY_CONCURRENCY=value)
        )


def test_configuration_is_immutable(tmp_path):
    config = WorkerConfig.from_environment(valid_env(tmp_path))

    with pytest.raises(FrozenInstanceError):
        config.max_attempts = 10
