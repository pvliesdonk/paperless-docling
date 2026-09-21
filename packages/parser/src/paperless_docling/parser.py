from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, Self

from paperless_docling import __version__
from paperless_docling.compatibility import (
    allow_unsupported_paperless,
    ensure_paperless_compatible,
)
from paperless_docling.raster import RasterAdapter, RasterResult

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from paperless.parsers import MetadataEntry, ParserContext


class ConversionSession(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    def convert(self, source: Path) -> str: ...


class ConversionFactory(Protocol):
    def __call__(self) -> ConversionSession: ...


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

    conversion_factory: ClassVar[ConversionFactory | None] = None
    raster_adapter_factory: ClassVar[Callable[..., RasterAdapter]] = RasterAdapter

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
        self.context: ParserContext | None = None
        self._conversion_context: ConversionSession | None = None
        self._conversion: ConversionSession | None = None
        self._raster_adapter: RasterAdapter | None = None
        self._raster_result: RasterResult | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if self._conversion_context is not None:
                self._conversion_context.__exit__(exc_type, exc_val, exc_tb)
        finally:
            shutil.rmtree(self.tempdir, ignore_errors=True)

    def configure(self, context: ParserContext) -> None:
        self.context = context
        if self._raster_adapter is not None:
            self._raster_adapter.configure(context)

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        self.archive_path = None
        self._raster_result = None

        conversion = self._get_conversion()
        try:
            markdown = conversion.convert(document_path)
            if not isinstance(markdown, str):
                raise TypeError("conversion did not return Markdown text")
        except Exception as error:
            raise _parse_error("Docling conversion failed.") from error

        self.text = markdown.strip()

        try:
            result = self._get_raster_adapter().parse(
                document_path,
                mime_type,
                produce_archive,
            )
        except Exception as error:
            if produce_archive:
                message = "Required archive generation failed."
            else:
                message = "Paperless raster processing failed."
            raise _parse_error(message) from error

        self._raster_result = result
        self.archive_path = result.archive_path

    def get_text(self) -> str:
        return self.text

    def get_date(self) -> datetime.datetime | None:
        return self.date

    def get_archive_path(self) -> Path | None:
        return self.archive_path

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        if self._raster_result is not None:
            return self._raster_result.thumbnail_path
        return self._get_raster_adapter().get_thumbnail(document_path, mime_type)

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        if self._raster_result is not None:
            return self._raster_result.page_count
        return self._get_raster_adapter().get_page_count(document_path, mime_type)

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        try:
            return self._get_raster_adapter().extract_metadata(
                document_path,
                mime_type,
            )
        except Exception:
            return []

    def _get_conversion(self) -> ConversionSession:
        if self._conversion is not None:
            return self._conversion

        factory = DoclingParser.conversion_factory
        if factory is None:
            raise _parse_error(
                "Docling conversion is unavailable; install the conversion "
                "client and configure its parser wiring.",
            )

        try:
            conversion_context = factory()
            conversion = conversion_context.__enter__()
        except Exception as error:
            raise _parse_error("Docling conversion failed.") from error
        self._conversion_context = conversion_context
        self._conversion = conversion
        return conversion

    def _get_raster_adapter(self) -> RasterAdapter:
        if self._raster_adapter is None:
            self._raster_adapter = type(self).raster_adapter_factory(
                output_dir=self.tempdir,
                logging_group=self.logging_group,
            )
            if self.context is not None:
                self._raster_adapter.configure(self.context)
        return self._raster_adapter


def _parse_error(message: str) -> Exception:
    from documents.parsers import ParseError

    return ParseError(message)
