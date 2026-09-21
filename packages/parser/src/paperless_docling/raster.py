from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from types import TracebackType

    from paperless.parsers import MetadataEntry, ParserContext

logger = logging.getLogger(__name__)


class RasterDelegate(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def configure(self, context: ParserContext) -> None: ...

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None: ...

    def get_archive_path(self) -> Path | None: ...

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path: ...

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None: ...

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]: ...


@dataclass(frozen=True, slots=True)
class RasterResult:
    archive_path: Path | None
    thumbnail_path: Path
    page_count: int | None


def _paperless_delegate(logging_group: object | None) -> RasterDelegate:
    from paperless.parsers.tesseract import RasterisedDocumentParser

    return RasterisedDocumentParser(logging_group)


class RasterAdapter:
    def __init__(
        self,
        *,
        output_dir: Path,
        logging_group: object | None = None,
        delegate_factory: Callable[[object | None], RasterDelegate] = (
            _paperless_delegate
        ),
    ) -> None:
        self.output_dir = output_dir
        self.logging_group = logging_group
        self.delegate_factory = delegate_factory
        self.context: ParserContext | None = None

    def configure(self, context: ParserContext) -> None:
        self.context = context

    def parse(
        self,
        source: Path,
        mime_type: str,
        produce_archive: bool,
    ) -> RasterResult:
        with self._delegate() as delegate:
            archive_path = None
            if produce_archive:
                delegate.parse(source, mime_type, produce_archive=True)
                generated_archive = delegate.get_archive_path()
                if generated_archive is None:
                    raise RuntimeError(
                        "Paperless raster parser did not produce an archive.",
                    )
                archive_path = self._copy_artifact(
                    generated_archive,
                    "archive.pdf",
                )

            thumbnail_path = self._copy_artifact(
                delegate.get_thumbnail(source, mime_type),
                "thumbnail.webp",
            )
            page_count = delegate.get_page_count(source, mime_type)

        return RasterResult(archive_path, thumbnail_path, page_count)

    def get_thumbnail(self, source: Path, mime_type: str) -> Path:
        with self._delegate() as delegate:
            return self._copy_artifact(
                delegate.get_thumbnail(source, mime_type),
                "thumbnail.webp",
            )

    def get_page_count(self, source: Path, mime_type: str) -> int | None:
        with self._delegate() as delegate:
            return delegate.get_page_count(source, mime_type)

    def extract_metadata(
        self,
        source: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        try:
            with self._delegate() as delegate:
                return delegate.extract_metadata(source, mime_type)
        except Exception:
            logger.warning("Unable to extract optional document metadata", exc_info=True)
            return []

    @contextmanager
    def _delegate(self) -> Iterator[RasterDelegate]:
        with self.delegate_factory(self.logging_group) as delegate:
            if self.context is not None:
                delegate.configure(self.context)
            yield delegate

    def _copy_artifact(self, source: Path, filename: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / filename
        shutil.copy2(source, destination)
        return destination
