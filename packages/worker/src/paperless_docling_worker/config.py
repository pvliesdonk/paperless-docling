from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr

from .credentials import is_valid_credential

DEFAULT_CONTEXT_TOKENS = 32_768
DEFAULT_OUTPUT_TOKENS = 1_024
DEFAULT_CHUNK_TOKENS = 12_000
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 300
DEFAULT_RETRY_BASE_SECONDS = 5
DEFAULT_RETRY_MAX_SECONDS = 300
DEFAULT_RETRY_JITTER_SECONDS = 1
DEFAULT_MAX_REQUEST_BYTES = 65_536
DEFAULT_CONCURRENCY = 1

MAX_CONTEXT_TOKENS = 1_000_000
MAX_OUTPUT_TOKENS = 100_000
MAX_CHUNK_TOKENS = 900_000
MAX_MAX_ATTEMPTS = 100
MAX_LEASE_SECONDS = 3_600
MAX_RETRY_BASE_SECONDS = 3_600
MAX_RETRY_SECONDS = 86_400
MAX_RETRY_JITTER_SECONDS = 3_600
MAX_REQUEST_BYTES = 1_048_576

_ACCESS_SUPPORTS_EFFECTIVE_IDS = os.access in os.supports_effective_ids


class ConfigurationError(ValueError):
    """Raised when worker startup configuration is invalid."""


@dataclass(frozen=True)
class WorkerConfig:
    paperless_url: str
    paperless_token_file: Path
    webhook_token_file: Path
    database_path: Path
    llm_base_url: str
    llm_api_key_file: Path
    llm_model: str
    profile_version: str
    context_tokens: int
    output_tokens: int
    chunk_tokens: int
    max_attempts: int
    lease_seconds: int
    retry_base_seconds: int
    retry_max_seconds: int
    retry_jitter_seconds: int
    max_request_bytes: int
    concurrency: int
    paperless_token: SecretStr = field(repr=False, compare=False)
    webhook_token: SecretStr = field(repr=False, compare=False)
    llm_api_key: SecretStr = field(repr=False, compare=False)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] = os.environ,
    ) -> WorkerConfig:
        paperless_url = _http_url(environ, "PAPERLESS_URL")
        llm_base_url = _http_url(environ, "SUMMARY_LLM_BASE_URL")
        paperless_token_file, paperless_token = _secret(
            environ, "PAPERLESS_TOKEN_FILE"
        )
        webhook_token_file, webhook_token = _secret(environ, "WEBHOOK_TOKEN_FILE")
        if not is_valid_credential(webhook_token.get_secret_value()):
            raise ConfigurationError(
                "WEBHOOK_TOKEN_FILE must contain a non-whitespace credential."
            )
        llm_api_key_file, llm_api_key = _secret(
            environ, "SUMMARY_LLM_API_KEY_FILE"
        )
        database_path = Path(_required(environ, "SUMMARY_DATABASE_PATH"))
        _validate_database_path(database_path)

        context_tokens = _positive_integer(
            environ,
            "SUMMARY_CONTEXT_TOKENS",
            default=DEFAULT_CONTEXT_TOKENS,
            maximum=MAX_CONTEXT_TOKENS,
        )
        output_tokens = _positive_integer(
            environ,
            "SUMMARY_OUTPUT_TOKENS",
            default=DEFAULT_OUTPUT_TOKENS,
            maximum=MAX_OUTPUT_TOKENS,
        )
        chunk_tokens = _positive_integer(
            environ,
            "SUMMARY_CHUNK_TOKENS",
            default=DEFAULT_CHUNK_TOKENS,
            maximum=MAX_CHUNK_TOKENS,
        )
        if chunk_tokens >= context_tokens - output_tokens:
            raise ConfigurationError(
                "SUMMARY_CHUNK_TOKENS must be less than "
                "SUMMARY_CONTEXT_TOKENS minus SUMMARY_OUTPUT_TOKENS."
            )

        retry_base_seconds = _positive_integer(
            environ,
            "SUMMARY_RETRY_BASE_SECONDS",
            default=DEFAULT_RETRY_BASE_SECONDS,
            maximum=MAX_RETRY_BASE_SECONDS,
        )
        retry_max_seconds = _positive_integer(
            environ,
            "SUMMARY_RETRY_MAX_SECONDS",
            default=DEFAULT_RETRY_MAX_SECONDS,
            maximum=MAX_RETRY_SECONDS,
        )
        retry_jitter_seconds = _positive_integer(
            environ,
            "SUMMARY_RETRY_JITTER_SECONDS",
            default=DEFAULT_RETRY_JITTER_SECONDS,
            maximum=MAX_RETRY_JITTER_SECONDS,
        )
        if retry_base_seconds > retry_max_seconds:
            raise ConfigurationError(
                "SUMMARY_RETRY_MAX_SECONDS must be at least "
                "SUMMARY_RETRY_BASE_SECONDS."
            )
        if retry_jitter_seconds > retry_max_seconds:
            raise ConfigurationError(
                "SUMMARY_RETRY_MAX_SECONDS must cap SUMMARY_RETRY_JITTER_SECONDS."
            )

        concurrency = _positive_integer(
            environ,
            "SUMMARY_CONCURRENCY",
            default=DEFAULT_CONCURRENCY,
            maximum=DEFAULT_CONCURRENCY,
        )

        return cls(
            paperless_url=paperless_url,
            paperless_token_file=paperless_token_file,
            webhook_token_file=webhook_token_file,
            database_path=database_path,
            llm_base_url=llm_base_url,
            llm_api_key_file=llm_api_key_file,
            llm_model=_required(environ, "SUMMARY_LLM_MODEL"),
            profile_version=_required(environ, "SUMMARY_PROFILE_VERSION"),
            context_tokens=context_tokens,
            output_tokens=output_tokens,
            chunk_tokens=chunk_tokens,
            max_attempts=_positive_integer(
                environ,
                "SUMMARY_MAX_ATTEMPTS",
                default=DEFAULT_MAX_ATTEMPTS,
                maximum=MAX_MAX_ATTEMPTS,
            ),
            lease_seconds=_positive_integer(
                environ,
                "SUMMARY_LEASE_SECONDS",
                default=DEFAULT_LEASE_SECONDS,
                maximum=MAX_LEASE_SECONDS,
            ),
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            retry_jitter_seconds=retry_jitter_seconds,
            max_request_bytes=_positive_integer(
                environ,
                "SUMMARY_MAX_REQUEST_BYTES",
                default=DEFAULT_MAX_REQUEST_BYTES,
                maximum=MAX_REQUEST_BYTES,
            ),
            concurrency=concurrency,
            paperless_token=paperless_token,
            webhook_token=webhook_token,
            llm_api_key=llm_api_key,
        )


def _required(environ: Mapping[str, str], variable: str) -> str:
    value = environ.get(variable)
    if value is None or not value.strip():
        raise ConfigurationError(f"{variable} must be non-empty.")
    return value


def _http_url(environ: Mapping[str, str], variable: str) -> str:
    value = _required(environ, variable)
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ConfigurationError(
            f"{variable} must be a valid HTTP(S) URL."
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError(f"{variable} must be an HTTP(S) URL.")
    return value


def _secret(
    environ: Mapping[str, str],
    variable: str,
) -> tuple[Path, SecretStr]:
    secret_file = Path(_required(environ, variable))
    try:
        if not secret_file.is_file():
            raise OSError
        value = secret_file.read_text(encoding="utf-8", newline="")
    except (OSError, UnicodeError):
        raise ConfigurationError(
            f"{variable} must be a readable text file."
        ) from None
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise ConfigurationError(f"{variable} must contain a secret.")
    return secret_file, SecretStr(value)


def _positive_integer(
    environ: Mapping[str, str],
    variable: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = environ.get(variable)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigurationError(
            f"{variable} must be a positive integer within its limit."
        ) from None
    if str(number) != value or number <= 0 or number > maximum:
        raise ConfigurationError(
            f"{variable} must be a positive integer within its limit."
        )
    return number


def _validate_database_path(database_path: Path) -> None:
    if not database_path.is_absolute():
        raise ConfigurationError("SUMMARY_DATABASE_PATH must be absolute.")
    if database_path.exists() and not database_path.is_file():
        raise ConfigurationError("SUMMARY_DATABASE_PATH must identify a file.")

    writable_location = database_path.parent
    while True:
        try:
            writable_location.lstat()
        except FileNotFoundError:
            writable_location = writable_location.parent
            continue
        except OSError:
            raise ConfigurationError(
                "SUMMARY_DATABASE_PATH must have directory ancestors."
            ) from None
        break
    if not writable_location.is_dir():
        raise ConfigurationError(
            "SUMMARY_DATABASE_PATH must have directory ancestors."
        )

    access_options = (
        {"effective_ids": True} if _ACCESS_SUPPORTS_EFFECTIVE_IDS else {}
    )
    if not os.access(writable_location, os.W_OK | os.X_OK, **access_options):
        raise ConfigurationError("SUMMARY_DATABASE_PATH parent must be writable.")
