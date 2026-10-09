from __future__ import annotations

import shutil
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self

from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.service_client import DoclingServiceClient
from docling.service_client.client import StatusWatcherKind

from paperless_docling import __version__
from paperless_docling.compatibility import (
    allow_unsupported_paperless,
    ensure_paperless_compatible,
)
from paperless_docling.config import PluginConfig

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from paperless.parsers import MetadataEntry, ParserContext


_SUPPORTED_MIME_TYPES = {
    # PDF
    "application/pdf": ".pdf",

    # Microsoft Office
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",

    # RTF
    "application/rtf": ".rtf",

    # OpenDocument
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",

    # HTML
    "text/html": ".html",
    "application/xhtml+xml": ".html",

    # MHTML
    "application/x-mimearchive": ".mhtml",
    "multipart/related": ".mhtml",

    # CSV
    "text/csv": ".csv",

    # Images
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


class DoclingParser:
    name = "Paperless Docling Parser"
    version = __version__
    author = "lockdlock"
    url = "https://github.com/lockdlock/paperless-docling"
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
            try:
                return int(__import__("os").environ.get("PAPERLESS_DOCLING_SCORE", "15"))
            except ValueError:
                return 15
        return None

    @property
    def can_produce_archive(self) -> bool:
        return True

    @property
    def requires_pdf_rendition(self) -> bool:
        return True

    def __init__(self, logging_group: object | None = None) -> None:
        ensure_paperless_compatible(
            allow_unsupported=allow_unsupported_paperless(),
        )

        self.logging_group = logging_group
        self.config = PluginConfig.from_environment()

        from django.conf import settings

        settings.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        self.tempdir = Path(
            __import__("tempfile").mkdtemp(
                prefix="paperless-docling-",
                dir=settings.SCRATCH_DIR,
            ),
        )

        self.text = ""
        self.archive_path: Path | None = None
        self.date: datetime.datetime | None = None
        self._exit_stack = ExitStack()
        self._gotenberg_client: GotenbergClient | None = None

    def __enter__(self) -> Self:
        from django.conf import settings
        from gotenberg_client import GotenbergClient

        self._gotenberg_client = self._exit_stack.enter_context(
            GotenbergClient(
                host=settings.TIKA_GOTENBERG_ENDPOINT,
                timeout=settings.CELERY_TASK_TIME_LIMIT,
            ),
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._exit_stack.close()
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
        cache_path = self._cache_input(document_path)

        try:
            options = ConvertDocumentsOptions(
                from_formats=list(self.config.from_formats),
                to_formats=list(self.config.to_formats),
                pipeline=self.config.pipeline,
                do_ocr=self.config.do_ocr,
                force_ocr=self.config.force_ocr,
                ocr_preset=self.config.ocr_preset,
                ocr_lang=list(self.config.ocr_lang),
                include_images=self.config.include_images,
                include_page_images=self.config.include_page_images,
                images_scale=self.config.images_scale,
                do_table_structure=self.config.do_table_structure,
                table_mode=self.config.table_mode,
                table_cell_matching=self.config.table_cell_matching,
                do_pdf_heading_hierarchy=self.config.do_pdf_heading_hierarchy,
                image_export_mode=self.config.image_export_mode,
                md_page_break_placeholder=self.config.md_page_break_placeholder,
                md_compact_tables=self.config.md_compact_tables,
            )

            with DoclingServiceClient(
                url=self.config.serve_url,
                status_watcher=StatusWatcherKind.POLLING,
                job_timeout=self.config.deadline.total_seconds(),
            ) as client:
                result = client.convert(
                    source=cache_path,
                    options=options,
                )

            if result is None:
                raise RuntimeError(
                    "Docling Serve returned no conversion result.",
                )

            self.text = result.document.export_to_markdown()

            if produce_archive and mime_type != "application/pdf":
                self.archive_path = self._convert_to_pdf(document_path)
            elif produce_archive and mime_type == "application/pdf":
                self.archive_path = document_path

        finally:
            with suppress(OSError):
                cache_path.unlink()

    def _cache_input(self, document_path: Path) -> Path:
        cache_dir = self.config.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

        suffix = document_path.suffix
        cache_path = (
            cache_dir
            / f"{document_path.stem}-{next(__import__('tempfile')._get_candidate_names())}{suffix}"
        )

        shutil.copy2(document_path, cache_path)

        return cache_path

    def _convert_to_pdf(self, document_path: Path) -> Path:
        from gotenberg_client.options import PdfAFormat

        from documents.parsers import ParseError
        from paperless.config import OutputTypeConfig
        from paperless.models import OutputTypeChoices
        if self._gotenberg_client is None:
            raise RuntimeError(
                "Gotenberg client is not initialized. "
                "DoclingParser must be used as a context manager.",
            )

        pdf_path = self.tempdir / "convert.pdf"

        with self._gotenberg_client.libre_office.to_pdf() as route:
            route.update_indexes(update_indexes=False)

            output_type = OutputTypeConfig().output_type

            if output_type in {
                OutputTypeChoices.PDF_A,
                OutputTypeChoices.PDF_A2,
            }:
                route.pdf_format(PdfAFormat.A2b)
            elif output_type == OutputTypeChoices.PDF_A1:
                route.pdf_format(PdfAFormat.A2b)
            elif output_type == OutputTypeChoices.PDF_A3:
                route.pdf_format(PdfAFormat.A3b)

            route.convert(document_path)

            try:
                response = route.run()
                pdf_path.write_bytes(response.content)
                return pdf_path
            except Exception as err:
                raise ParseError(
                    f"Error while converting document to PDF: {err}",
                ) from err

    def get_text(self) -> str:
        return self.text

    def get_date(self) -> datetime.datetime | None:
        return self.date

    def get_archive_path(self) -> Path | None:
        return self.archive_path

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        from documents.parsers import make_thumbnail_from_pdf

        return make_thumbnail_from_pdf(
            self.archive_path or document_path,
            self.tempdir,
        )

    def get_page_count(
        self,
        document_path: Path,
        mime_type: str,
    ) -> int | None:
        from paperless.parsers.utils import get_page_count_for_pdf

        if mime_type == "application/pdf":
            return get_page_count_for_pdf(document_path)
        if self.archive_path is not None:
            return get_page_count_for_pdf(self.archive_path)
        return None

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        from paperless.parsers.utils import extract_pdf_metadata

        if mime_type == "application/pdf":
            return extract_pdf_metadata(document_path)
        if self.archive_path is not None:
            return extract_pdf_metadata(self.archive_path)
        return []
