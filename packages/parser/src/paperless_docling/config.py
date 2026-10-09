from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from docling.datamodel.base_models import InputFormat, OutputFormat
from docling.datamodel.pipeline_options import ProcessingPipeline, TableFormerMode
from docling_core.types.doc.base import ImageRefMode

from paperless_docling.compatibility import allow_unsupported_paperless
from paperless_docling.errors import ConfigurationError, IncompatiblePaperlessError

DEFAULT_PRESET = "paperless-vlm"
DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_DEADLINE = timedelta(minutes=10)
DEFAULT_CACHE_MAX_AGE = timedelta(days=30)
DEFAULT_CACHE_MAX_BYTES = 1_073_741_824

MAX_POLL_INTERVAL = timedelta(seconds=60)
MAX_DEADLINE = timedelta(hours=24)
MAX_CACHE_MAX_AGE = timedelta(days=365)
MAX_CACHE_MAX_BYTES = 1_099_511_627_776

_ACCESS_SUPPORTS_EFFECTIVE_IDS = os.access in os.supports_effective_ids


@dataclass(frozen=True)
class PluginConfig:
    serve_url: str
    token_file: Path | None
    preset: str
    profile_version: str

    from_formats: tuple[InputFormat, ...]
    to_formats: tuple[OutputFormat, ...]
    pipeline: ProcessingPipeline

    ocr_preset: str
    ocr_lang: tuple[str, ...]
    do_ocr: bool
    force_ocr: bool

    include_images: bool
    include_page_images: bool
    images_scale: float

    do_table_structure: bool
    table_mode: TableFormerMode
    table_cell_matching: bool

    do_pdf_heading_hierarchy: bool

    image_export_mode: ImageRefMode
    md_page_break_placeholder: str
    md_compact_tables: bool

    poll_interval: timedelta
    deadline: timedelta
    cache_dir: Path
    cache_max_age: timedelta
    cache_max_bytes: int
    allow_unsupported_paperless: bool
    _token: str | None = field(repr=False, compare=False)

    @property
    def token(self) -> str | None:
        return self._token

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] = os.environ,
    ) -> PluginConfig:
        serve_url = _required(environ, "PAPERLESS_DOCLING_SERVE_URL")

        try:
            parsed_url = urlparse(serve_url)
            hostname = parsed_url.hostname
            parsed_url.port
        except ValueError:
            raise ConfigurationError(
                "PAPERLESS_DOCLING_SERVE_URL must be a valid HTTP(S) URL.",
            ) from None

        if (
            parsed_url.scheme not in {"http", "https"}
            or not hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ConfigurationError(
                "PAPERLESS_DOCLING_SERVE_URL must be an HTTP(S) URL.",
            )

        profile_version = _required(
            environ,
            "PAPERLESS_DOCLING_PROFILE_VERSION",
        )

        cache_dir = Path(_required(environ, "PAPERLESS_DOCLING_CACHE_DIR"))
        _validate_cache_dir(cache_dir, "PAPERLESS_DOCLING_CACHE_DIR")

        preset = environ.get("PAPERLESS_DOCLING_PRESET", DEFAULT_PRESET)
        if not preset.strip():
            raise ConfigurationError(
                "PAPERLESS_DOCLING_PRESET must be non-empty.",
            )

        token_file, token = _read_token(environ)

        from_formats = _required_enum_list(
            environ,
            "PAPERLESS_DOCLING_FROM_FORMATS",
            InputFormat,
        )

        to_formats = _required_enum_list(
            environ,
            "PAPERLESS_DOCLING_TO_FORMATS",
            OutputFormat,
        )

        pipeline = _required_enum_value(
            environ,
            "PAPERLESS_DOCLING_PIPELINE",
            ProcessingPipeline,
        )

        ocr_preset = _required(
            environ,
            "PAPERLESS_DOCLING_OCR_PRESET",
        )

        ocr_lang = _required_string_list(
            environ,
            "PAPERLESS_DOCLING_OCR_LANG",
        )

        do_ocr = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_DO_OCR",
        )

        force_ocr = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_FORCE_OCR",
        )

        include_images = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_INCLUDE_IMAGES",
        )

        include_page_images = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_INCLUDE_PAGE_IMAGES",
        )

        images_scale = _required_positive_float(
            environ,
            "PAPERLESS_DOCLING_IMAGES_SCALE",
        )

        do_table_structure = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_DO_TABLE_STRUCTURE",
        )

        table_mode = _required_enum_value(
            environ,
            "PAPERLESS_DOCLING_TABLE_MODE",
            TableFormerMode,
        )

        table_cell_matching = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_TABLE_CELL_MATCHING",
        )

        do_pdf_heading_hierarchy = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_DO_PDF_HEADING_HIERARCHY",
        )

        image_export_mode = _required_enum_value(
            environ,
            "PAPERLESS_DOCLING_IMAGE_EXPORT_MODE",
            ImageRefMode,
        )

        md_page_break_placeholder = _required_exact(
            environ,
            "PAPERLESS_DOCLING_MD_PAGE_BREAK_PLACEHOLDER",
        )

        md_compact_tables = _required_boolean(
            environ,
            "PAPERLESS_DOCLING_MD_COMPACT_TABLES",
        )

        poll_interval = _duration(
            environ,
            "PAPERLESS_DOCLING_POLL_INTERVAL_SECONDS",
            default=DEFAULT_POLL_INTERVAL,
            maximum=MAX_POLL_INTERVAL,
            unit="seconds",
        )

        deadline = _duration(
            environ,
            "PAPERLESS_DOCLING_DEADLINE_SECONDS",
            default=DEFAULT_DEADLINE,
            maximum=MAX_DEADLINE,
            unit="seconds",
        )

        cache_max_age = _duration(
            environ,
            "PAPERLESS_DOCLING_CACHE_MAX_AGE_DAYS",
            default=DEFAULT_CACHE_MAX_AGE,
            maximum=MAX_CACHE_MAX_AGE,
            unit="days",
        )

        cache_max_bytes = _positive_integer(
            environ,
            "PAPERLESS_DOCLING_CACHE_MAX_BYTES",
            default=DEFAULT_CACHE_MAX_BYTES,
            maximum=MAX_CACHE_MAX_BYTES,
        )

        try:
            allow_unsupported = allow_unsupported_paperless(environ)
        except IncompatiblePaperlessError as error:
            raise ConfigurationError(str(error)) from error

        return cls(
            serve_url=serve_url,
            token_file=token_file,
            preset=preset,
            profile_version=profile_version,
            from_formats=from_formats,
            to_formats=to_formats,
            pipeline=pipeline,
            ocr_preset=ocr_preset,
            ocr_lang=ocr_lang,
            do_ocr=do_ocr,
            force_ocr=force_ocr,
            include_images=include_images,
            include_page_images=include_page_images,
            images_scale=images_scale,
            do_table_structure=do_table_structure,
            table_mode=table_mode,
            table_cell_matching=table_cell_matching,
            do_pdf_heading_hierarchy=do_pdf_heading_hierarchy,
            image_export_mode=image_export_mode,
            md_page_break_placeholder=md_page_break_placeholder,
            md_compact_tables=md_compact_tables,
            poll_interval=poll_interval,
            deadline=deadline,
            cache_dir=cache_dir,
            cache_max_age=cache_max_age,
            cache_max_bytes=cache_max_bytes,
            allow_unsupported_paperless=allow_unsupported,
            _token=token,
        )


def _required(environ: Mapping[str, str], variable: str) -> str:
    value = environ.get(variable)
    if value is None or not value.strip():
        raise ConfigurationError(f"{variable} must be non-empty.")
    return value


def _required_exact(environ: Mapping[str, str], variable: str) -> str:
    if variable not in environ:
        raise ConfigurationError(f"{variable} must be defined.")
    return environ[variable]


def _required_string_list(
    environ: Mapping[str, str],
    variable: str,
) -> tuple[str, ...]:
    value = _required(environ, variable)
    items = tuple(item.strip() for item in value.split(",") if item.strip())

    if not items:
        raise ConfigurationError(
            f"{variable} must contain at least one value.",
        )

    return items


def _required_enum_list(
    environ: Mapping[str, str],
    variable: str,
    enum_type: type,
) -> tuple:
    names = _required_string_list(environ, variable)

    valid = {member.value: member for member in enum_type}
    result = []

    for name in names:
        member = valid.get(name)
        if member is None:
            allowed = ", ".join(sorted(valid))
            raise ConfigurationError(
                f"{variable} contains invalid value {name!r}. "
                f"Allowed values: {allowed}.",
            )
        result.append(member)

    return tuple(result)


def _required_enum_value(
    environ: Mapping[str, str],
    variable: str,
    enum_type: type,
):
    value = _required(environ, variable)

    valid = {member.value: member for member in enum_type}
    member = valid.get(value)

    if member is None:
        allowed = ", ".join(sorted(valid))
        raise ConfigurationError(
            f"{variable} contains invalid value {value!r}. "
            f"Allowed values: {allowed}.",
        )

    return member


def _required_boolean(
    environ: Mapping[str, str],
    variable: str,
) -> bool:
    value = _required(environ, variable).lower()

    if value in {"true", "1", "yes", "on"}:
        return True

    if value in {"false", "0", "no", "off"}:
        return False

    raise ConfigurationError(
        f"{variable} must be a boolean.",
    )


def _required_positive_float(
    environ: Mapping[str, str],
    variable: str,
) -> float:
    number = _decimal(_required(environ, variable), variable)

    if number <= 0:
        raise ConfigurationError(
            f"{variable} must be positive.",
        )

    return float(number)


def _read_token(environ: Mapping[str, str]) -> tuple[Path | None, str | None]:
    value = environ.get("PAPERLESS_DOCLING_SERVE_TOKEN_FILE")
    if value is None:
        return None, None

    token_file = Path(value)

    try:
        if not token_file.is_file():
            raise OSError
        token = token_file.read_text()
    except (OSError, UnicodeError):
        raise ConfigurationError(
            "PAPERLESS_DOCLING_SERVE_TOKEN_FILE must be a readable text file.",
        ) from None

    if token.endswith("\n"):
        token = token[:-1]

    return token_file, token


def _duration(
    environ: Mapping[str, str],
    variable: str,
    *,
    default: timedelta,
    maximum: timedelta,
    unit: str,
) -> timedelta:
    value = environ.get(variable)
    if value is None:
        return default

    number = _decimal(value, variable)
    maximum_value = Decimal(str(maximum.total_seconds()))

    if unit == "days":
        maximum_value /= Decimal(86_400)

    if number <= 0 or number > maximum_value:
        raise ConfigurationError(
            f"{variable} must be positive and within its limit.",
        )

    duration = timedelta(**{unit: float(number)})

    if duration <= timedelta(0):
        raise ConfigurationError(
            f"{variable} must resolve to a positive duration.",
        )

    return duration


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

    number = _decimal(value, variable)

    if (
        number != number.to_integral_value()
        or number <= 0
        or number > maximum
    ):
        raise ConfigurationError(
            f"{variable} must be a positive integer within its limit.",
        )

    return int(number)


def _decimal(value: str, variable: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ConfigurationError(
            f"{variable} must be numeric.",
        ) from error

    if not number.is_finite():
        raise ConfigurationError(
            f"{variable} must be numeric.",
        )

    return number


def _validate_cache_dir(
    cache_dir: Path,
    env_name: str = "PAPERLESS_DOCLING_CACHE_DIR",
) -> None:
    if not cache_dir.is_absolute():
        raise ConfigurationError(
            f"{env_name} must be absolute.",
        )

    writable_location = cache_dir

    while True:
        try:
            writable_location.lstat()
        except FileNotFoundError:
            writable_location = writable_location.parent
            continue
        except OSError as error:
            raise ConfigurationError(
                f"{env_name} must have directory ancestors.",
            ) from error
        break

    if not writable_location.is_dir():
        raise ConfigurationError(
            f"{env_name} must have directory ancestors.",
        )

    access_options = (
        {"effective_ids": True}
        if _ACCESS_SUPPORTS_EFFECTIVE_IDS
        else {}
    )

    if not os.access(
        writable_location,
        os.W_OK | os.X_OK,
        **access_options,
    ):
        raise ConfigurationError(
            f"{env_name} must be writable.",
        )
