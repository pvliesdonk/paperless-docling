from pathlib import Path

import pytest
from paperless_docling.raster import RasterAdapter


class FakeRasterParser:
    def __init__(
        self,
        tempdir: Path,
        *,
        archive: bool = True,
        parse_error: Exception | None = None,
        thumbnail_error: Exception | None = None,
        page_count_error: Exception | None = None,
        metadata_error: Exception | None = None,
    ) -> None:
        self.tempdir = tempdir
        self.archive = archive
        self.parse_error = parse_error
        self.thumbnail_error = thumbnail_error
        self.page_count_error = page_count_error
        self.metadata_error = metadata_error
        self.parse_calls = []
        self.configured_with = []
        self.exited = False
        self.text = "raster text must not escape"
        self.archive_path: Path | None = None

    def __enter__(self):
        self.tempdir.mkdir()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True

    def configure(self, context):
        self.configured_with.append(context)

    def parse(self, source, mime_type, *, produce_archive=True):
        self.parse_calls.append((source, mime_type, produce_archive))
        if self.parse_error is not None:
            raise self.parse_error
        if self.archive:
            self.archive_path = self.tempdir / "archive.pdf"
            self.archive_path.write_bytes(b"searchable archive")

    def get_archive_path(self):
        return self.archive_path

    def get_thumbnail(self, source, mime_type):
        if self.thumbnail_error is not None:
            raise self.thumbnail_error
        thumbnail = self.tempdir / "thumbnail.webp"
        thumbnail.write_bytes(b"thumbnail")
        return thumbnail

    def get_page_count(self, source, mime_type):
        if self.page_count_error is not None:
            raise self.page_count_error
        return 7

    def extract_metadata(self, source, mime_type):
        if self.metadata_error is not None:
            raise self.metadata_error
        return [{"key": "Author", "value": "Ada"}]


def adapter_with(delegate, output_dir):
    return RasterAdapter(
        output_dir=output_dir,
        delegate_factory=lambda _logging_group: delegate,
    )


def test_archive_policy_false_skips_raster_parse_but_keeps_derived_helpers(tmp_path):
    delegate = FakeRasterParser(tmp_path / "delegate")
    adapter = adapter_with(delegate, tmp_path / "output")

    result = adapter.parse(tmp_path / "source.pdf", "application/pdf", False)

    assert delegate.parse_calls == []
    assert result.archive_path is None
    assert result.thumbnail_path.read_bytes() == b"thumbnail"
    assert result.page_count == 7
    assert delegate.exited is True


def test_archive_policy_true_delegates_once_and_preserves_outputs(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    delegate = FakeRasterParser(tmp_path / "delegate")
    adapter = adapter_with(delegate, tmp_path / "output")
    context = object()
    adapter.configure(context)

    result = adapter.parse(source, "application/pdf", True)

    assert delegate.parse_calls == [(source, "application/pdf", True)]
    assert delegate.configured_with == [context]
    assert result.archive_path is not None
    assert result.archive_path.read_bytes() == b"searchable archive"
    assert result.thumbnail_path.read_bytes() == b"thumbnail"
    assert not hasattr(result, "text")
    assert delegate.exited is True


def test_born_digital_auto_policy_skips_archive_generation(tmp_path):
    delegate = FakeRasterParser(tmp_path / "delegate")
    adapter = adapter_with(delegate, tmp_path / "output")

    result = adapter.parse(tmp_path / "source.pdf", "application/pdf", False)

    assert result.archive_path is None
    assert delegate.parse_calls == []
    assert delegate.exited is True


def test_requested_archive_missing_from_delegate_is_failure(tmp_path):
    delegate = FakeRasterParser(tmp_path / "delegate", archive=False)
    adapter = adapter_with(delegate, tmp_path / "output")

    with pytest.raises(RuntimeError, match="did not produce an archive"):
        adapter.parse(tmp_path / "source.pdf", "application/pdf", True)

    assert delegate.exited is True


def test_delegate_closes_when_required_archive_generation_fails(tmp_path):
    delegate = FakeRasterParser(
        tmp_path / "delegate",
        parse_error=RuntimeError("ocr failed"),
    )
    adapter = adapter_with(delegate, tmp_path / "output")

    with pytest.raises(RuntimeError, match="ocr failed"):
        adapter.parse(tmp_path / "source.pdf", "application/pdf", True)

    assert delegate.exited is True


def test_independent_thumbnail_page_count_and_metadata_delegate_and_close(tmp_path):
    delegates = []

    def factory(_logging_group):
        delegate = FakeRasterParser(tmp_path / f"delegate-{len(delegates)}")
        delegates.append(delegate)
        return delegate

    adapter = RasterAdapter(
        output_dir=tmp_path / "output",
        delegate_factory=factory,
    )
    source = tmp_path / "source.pdf"

    thumbnail = adapter.get_thumbnail(source, "application/pdf")
    page_count = adapter.get_page_count(source, "application/pdf")
    metadata = adapter.extract_metadata(source, "application/pdf")

    assert thumbnail.read_bytes() == b"thumbnail"
    assert page_count == 7
    assert metadata == [{"key": "Author", "value": "Ada"}]
    assert len(delegates) == 3
    assert all(delegate.exited for delegate in delegates)


def test_optional_metadata_failure_returns_empty_result_and_closes(tmp_path):
    delegate = FakeRasterParser(
        tmp_path / "delegate",
        metadata_error=RuntimeError("bad metadata"),
    )
    adapter = adapter_with(delegate, tmp_path / "output")

    assert adapter.extract_metadata(tmp_path / "source.pdf", "application/pdf") == []
    assert delegate.exited is True


@pytest.mark.parametrize("operation", ["thumbnail", "page_count"])
def test_delegate_closes_when_independent_helper_fails(tmp_path, operation):
    error = RuntimeError("helper failed")
    delegate = FakeRasterParser(
        tmp_path / "delegate",
        thumbnail_error=error if operation == "thumbnail" else None,
        page_count_error=error if operation == "page_count" else None,
    )
    adapter = adapter_with(delegate, tmp_path / "output")

    with pytest.raises(RuntimeError, match="helper failed"):
        getattr(adapter, f"get_{operation}")(
            tmp_path / "source.pdf",
            "application/pdf",
        )

    assert delegate.exited is True
