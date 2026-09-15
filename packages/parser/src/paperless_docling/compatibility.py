import os
from collections.abc import Mapping

from paperless_docling import __version__
from paperless_docling.errors import IncompatiblePaperlessError

SUPPORTED_PAPERLESS_RANGE = ">=3.1,<3.2"
ALLOW_UNSUPPORTED_VARIABLE = "PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS"


def paperless_version() -> str:
    from paperless.version import __version__ as running_version

    if isinstance(running_version, tuple):
        return ".".join(str(part) for part in running_version)
    return str(running_version)


def allow_unsupported_paperless(
    environ: Mapping[str, str] = os.environ,
) -> bool:
    value = environ.get(ALLOW_UNSUPPORTED_VARIABLE, "false")
    if value == "true":
        return True
    if value == "false":
        return False
    raise IncompatiblePaperlessError(
        f"{ALLOW_UNSUPPORTED_VARIABLE} must be either 'true' or 'false'.",
    )


def _release_components(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if len(parts) < 2 or any(
        not part or not part.isascii() or not part.isdigit() for part in parts
    ):
        return None
    return tuple(int(part) for part in parts)


def ensure_paperless_compatible(*, allow_unsupported: bool) -> None:
    if allow_unsupported:
        return

    running_version = paperless_version()
    release = _release_components(running_version)
    is_supported = release is not None and release[:2] == (3, 1)

    if is_supported:
        return

    raise IncompatiblePaperlessError(
        f"Paperless-ngx {running_version} is incompatible with "
        f"paperless-docling {__version__}; supported Paperless-ngx versions are "
        f"{SUPPORTED_PAPERLESS_RANGE}. Set "
        "PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS=true only for deliberate "
        "staging tests.",
    )
