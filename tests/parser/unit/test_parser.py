import hashlib
from pathlib import Path

import pytest
from paperless_docling.parser import DoclingParser
from paperless_docling.raster import RasterResult


class FakeConversion:
    def __init__(self, markdown="# Docling\n\nContent", error=None):
        self.markdown = markdown
        self.error = error
        self.converted = []
        self.exit_exception = "not exited"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_exception = exc_value

    def convert(self, source):
        self.converted.append(source)
        if self.error is not None:
            raise self.error
        return self.markdown


class BrokenConversionContext(FakeConversion):
    def __enter__(self):
        raise RuntimeError("configuration failed")


class FakeRasterAdapter:
    def __init__(self, result=None, parse_error=None, metadata_error=None):
        self.result = result
        self.parse_error = parse_error
        self.metadata_error = metadata_error
        self.configured_with = []
        self.parse_calls = []
        self.thumbnail_calls = []
        self.page_count_calls = []
        self.metadata_calls = []

    def configure(self, context):
        self.configured_with.append(context)

    def parse(self, source, mime_type, produce_archive):
        self.parse_calls.append((source, mime_type, produce_archive))
        if self.parse_error is not None:
            raise self.parse_error
        return self.result

    def get_thumbnail(self, source, mime_type):
        self.thumbnail_calls.append((source, mime_type))
        return Path("delegated-thumbnail.webp")

    def get_page_count(self, source, mime_type):
        self.page_count_calls.append((source, mime_type))
        return 3

    def extract_metadata(self, source, mime_type):
        self.metadata_calls.append((source, mime_type))
        if self.metadata_error is not None:
            raise self.metadata_error
        return [{"key": "Title", "value": "Document"}]


def configure_dependencies(monkeypatch, conversion, raster):
    monkeypatch.setattr(DoclingParser, "conversion_factory", lambda: conversion)
    monkeypatch.setattr(
        DoclingParser,
        "raster_adapter_factory",
        lambda **_kwargs: raster,
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_parse_preserves_source_and_uses_only_docling_markdown(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"immutable source")
    original_digest = digest(source)
    archive = tmp_path / "archive.pdf"
    thumbnail = tmp_path / "thumbnail.webp"
    conversion = FakeConversion("\n# Docling\n\nContent\n")
    raster = FakeRasterAdapter(RasterResult(archive, thumbnail, 4))
    configure_dependencies(monkeypatch, conversion, raster)

    with DoclingParser() as parser:
        context = object()
        parser.configure(context)
        parser.parse(source, "application/pdf", produce_archive=True)

        assert parser.get_text() == "# Docling\n\nContent"
        assert parser.get_archive_path() == archive
        assert parser.get_thumbnail(source, "application/pdf") == thumbnail
        assert parser.get_page_count(source, "application/pdf") == 4
        assert parser.get_date() is None
        assert raster.configured_with == [context]

    assert digest(source) == original_digest
    assert raster.parse_calls == [(source, "application/pdf", True)]
    assert conversion.converted == [source]
    assert conversion.exit_exception is None


def test_parse_without_archive_preserves_source_and_forwards_policy(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.png"
    source.write_bytes(b"immutable image")
    original_digest = digest(source)
    conversion = FakeConversion()
    raster = FakeRasterAdapter(RasterResult(None, Path("thumb.webp"), 1))
    configure_dependencies(monkeypatch, conversion, raster)

    with DoclingParser() as parser:
        parser.parse(source, "image/png", produce_archive=False)
        assert parser.get_archive_path() is None

    assert digest(source) == original_digest
    assert raster.parse_calls == [(source, "image/png", False)]


def test_born_digital_auto_policy_does_not_request_archive(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "born-digital.pdf"
    source.write_bytes(b"pdf")
    conversion = FakeConversion()
    raster = FakeRasterAdapter(RasterResult(None, Path("thumb.webp"), 2))
    configure_dependencies(monkeypatch, conversion, raster)

    with DoclingParser() as parser:
        parser.parse(source, "application/pdf", produce_archive=False)
        assert parser.get_archive_path() is None

    assert raster.parse_calls == [(source, "application/pdf", False)]


def test_required_archive_failure_is_parse_error_and_preserves_source(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"immutable source")
    original_digest = digest(source)
    conversion = FakeConversion()
    raster = FakeRasterAdapter(parse_error=RuntimeError("ocr failed"))
    configure_dependencies(monkeypatch, conversion, raster)

    with pytest.raises(paperless_parse_error, match="archive generation failed"):
        with DoclingParser() as parser:
            parser.parse(source, "application/pdf", produce_archive=True)

    assert digest(source) == original_digest
    assert conversion.exit_exception is not None


def test_conversion_failure_is_parse_error_and_does_not_run_raster(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"immutable source")
    original_digest = digest(source)
    conversion = FakeConversion(error=RuntimeError("service failed"))
    raster = FakeRasterAdapter()
    configure_dependencies(monkeypatch, conversion, raster)

    with pytest.raises(paperless_parse_error, match="Docling conversion failed"):
        with DoclingParser() as parser:
            parser.parse(source, "application/pdf")

    assert digest(source) == original_digest
    assert raster.parse_calls == []


def test_conversion_setup_failure_is_translated_to_parse_error(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"immutable source")
    raster = FakeRasterAdapter()
    configure_dependencies(monkeypatch, BrokenConversionContext(), raster)

    with pytest.raises(paperless_parse_error, match="Docling conversion failed"):
        with DoclingParser() as parser:
            parser.parse(source, "application/pdf")

    assert raster.parse_calls == []


def test_missing_conversion_dependency_fails_selected_parse_clearly(
    tmp_path,
    monkeypatch,
    paperless_version_module,
    paperless_parse_error,
):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"source")
    monkeypatch.setattr(DoclingParser, "conversion_factory", None)

    with pytest.raises(paperless_parse_error, match="conversion is unavailable"):
        with DoclingParser() as parser:
            parser.parse(source, "application/pdf")


def test_accessors_delegate_when_parse_result_is_not_available(
    tmp_path,
    monkeypatch,
    paperless_version_module,
):
    raster = FakeRasterAdapter()
    monkeypatch.setattr(
        DoclingParser,
        "raster_adapter_factory",
        lambda **_kwargs: raster,
    )
    source = tmp_path / "document.pdf"

    with DoclingParser() as parser:
        assert parser.get_thumbnail(source, "application/pdf") == Path(
            "delegated-thumbnail.webp",
        )
        assert parser.get_page_count(source, "application/pdf") == 3
        assert parser.extract_metadata(source, "application/pdf") == [
            {"key": "Title", "value": "Document"},
        ]


def test_optional_metadata_failure_never_fails_consumption(
    tmp_path,
    monkeypatch,
    paperless_version_module,
):
    raster = FakeRasterAdapter(metadata_error=RuntimeError("metadata failed"))
    monkeypatch.setattr(
        DoclingParser,
        "raster_adapter_factory",
        lambda **_kwargs: raster,
    )

    with DoclingParser() as parser:
        assert parser.extract_metadata(tmp_path / "document.pdf", "application/pdf") == []


def test_later_context_failure_is_forwarded_to_conversion_session(
    monkeypatch,
    paperless_version_module,
):
    conversion = FakeConversion()
    raster = FakeRasterAdapter(RasterResult(None, Path("thumb.webp"), 1))
    configure_dependencies(monkeypatch, conversion, raster)

    with pytest.raises(RuntimeError, match="storage failed"):
        with DoclingParser() as parser:
            parser.parse(Path("document.pdf"), "application/pdf")
            raise RuntimeError("storage failed")

    assert isinstance(conversion.exit_exception, RuntimeError)
