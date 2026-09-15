from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Self

from paperless_docling import __version__
from paperless_docling.compatibility import (
    allow_unsupported_paperless,
    ensure_paperless_compatible,
)

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from paperless.parsers import MetadataEntry, ParserContext

_SUPPORTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class DoclingParser:
    name = "Paperless Docling Parser"
    version = __version__
    author = "Peter van Liesdonk"
    url = "https://github.com/pvliesdonk/paperless-docling"
    uses_remote_service = True

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _SUPPORTED_MIME_TYPES

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        if mime_type in _SUPPORTED_MIME_TYPES:
            return 20
        return None

    @property
    def can_produce_archive(self) -> bool:
        return True

    @property
    def requires_pdf_rendition(self) -> bool:
        return False

    def __init__(self, logging_group: object | None = None) -> None:
        ensure_paperless_compatible(
            allow_unsupported=allow_unsupported_paperless(),
        )
        self.logging_group = logging_group
        self.tempdir = Path(tempfile.mkdtemp(prefix="paperless-"))
        self.text = ""
        self.archive_path: Path | None = None
        self.date: datetime.datetime | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def configure(self, context: ParserContext) -> None:
        pass

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        raise NotImplementedError

    def get_text(self) -> str:
        return self.text

    def get_date(self) -> datetime.datetime | None:
        return self.date

    def get_archive_path(self) -> Path | None:
        return self.archive_path

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        raise NotImplementedError

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        raise NotImplementedError

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        raise NotImplementedError
