import inspect
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from paperless_docling.errors import IncompatiblePaperlessError
from paperless_docling.parser import DoclingParser

EXPECTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
PROJECT_ROOT = Path(__file__).parents[3]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install-parser.sh"
POSITIONAL = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY
REQUIRED = inspect.Parameter.empty
EXPECTED_PROTOCOL_SIGNATURES = {
    "supported_mime_types": (("cls", POSITIONAL, REQUIRED),),
    "score": (
        ("cls", POSITIONAL, REQUIRED),
        ("mime_type", POSITIONAL, REQUIRED),
        ("filename", POSITIONAL, REQUIRED),
        ("path", POSITIONAL, None),
    ),
    "can_produce_archive": (("self", POSITIONAL, REQUIRED),),
    "requires_pdf_rendition": (("self", POSITIONAL, REQUIRED),),
    "__init__": (
        ("self", POSITIONAL, REQUIRED),
        ("logging_group", POSITIONAL, None),
    ),
    "__enter__": (("self", POSITIONAL, REQUIRED),),
    "__exit__": (
        ("self", POSITIONAL, REQUIRED),
        ("exc_type", POSITIONAL, REQUIRED),
        ("exc_val", POSITIONAL, REQUIRED),
        ("exc_tb", POSITIONAL, REQUIRED),
    ),
    "configure": (
        ("self", POSITIONAL, REQUIRED),
        ("context", POSITIONAL, REQUIRED),
    ),
    "parse": (
        ("self", POSITIONAL, REQUIRED),
        ("document_path", POSITIONAL, REQUIRED),
        ("mime_type", POSITIONAL, REQUIRED),
        ("produce_archive", KEYWORD_ONLY, True),
    ),
    "get_text": (("self", POSITIONAL, REQUIRED),),
    "get_date": (("self", POSITIONAL, REQUIRED),),
    "get_archive_path": (("self", POSITIONAL, REQUIRED),),
    "get_thumbnail": (
        ("self", POSITIONAL, REQUIRED),
        ("document_path", POSITIONAL, REQUIRED),
        ("mime_type", POSITIONAL, REQUIRED),
    ),
    "get_page_count": (
        ("self", POSITIONAL, REQUIRED),
        ("document_path", POSITIONAL, REQUIRED),
        ("mime_type", POSITIONAL, REQUIRED),
    ),
    "extract_metadata": (
        ("self", POSITIONAL, REQUIRED),
        ("document_path", POSITIONAL, REQUIRED),
        ("mime_type", POSITIONAL, REQUIRED),
    ),
}


def test_parser_exposes_registry_identity():
    assert DoclingParser.name == "Paperless Docling Parser"
    assert DoclingParser.version == "0.1.0"
    assert DoclingParser.author
    assert DoclingParser.url == "https://github.com/pvliesdonk/paperless-docling"
    assert DoclingParser.uses_remote_service is True


def test_parser_supports_approved_raster_mime_types():
    assert DoclingParser.supported_mime_types() == EXPECTED_MIME_TYPES


@pytest.mark.parametrize("mime_type", EXPECTED_MIME_TYPES)
def test_parser_outscores_builtin_raster_parser(mime_type):
    assert DoclingParser.score(mime_type, "document", Path("document")) > 10


def test_parser_declines_unknown_mime_type():
    assert DoclingParser.score("application/zip", "archive.zip") is None


def test_parser_tests_do_not_install_global_paperless_fakes():
    assert "paperless" not in sys.modules
    assert "paperless.version" not in sys.modules


def test_parser_matches_paperless_3_1_call_signatures():
    missing = EXPECTED_PROTOCOL_SIGNATURES.keys() - DoclingParser.__dict__.keys()
    assert not missing, f"Missing Paperless parser protocol members: {sorted(missing)}"

    for name, expected in EXPECTED_PROTOCOL_SIGNATURES.items():
        descriptor = inspect.getattr_static(DoclingParser, name)
        if isinstance(descriptor, (classmethod, property)):
            function = (
                descriptor.__func__
                if isinstance(descriptor, classmethod)
                else descriptor.fget
            )
        else:
            function = descriptor
        assert function is not None
        actual = tuple(
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(function).parameters.values()
        )
        assert actual == expected, name

    assert isinstance(
        inspect.getattr_static(DoclingParser, "supported_mime_types"),
        classmethod,
    )
    assert isinstance(inspect.getattr_static(DoclingParser, "score"), classmethod)
    assert isinstance(
        inspect.getattr_static(DoclingParser, "can_produce_archive"),
        property,
    )
    assert isinstance(
        inspect.getattr_static(DoclingParser, "requires_pdf_rendition"),
        property,
    )


def test_parser_context_removes_temporary_directory_after_exception(
    paperless_version_module,
):
    tempdir = None

    with pytest.raises(RuntimeError, match="downstream failure"):
        with DoclingParser() as parser:
            tempdir = parser.tempdir
            assert tempdir.is_dir()
            raise RuntimeError("downstream failure")

    assert tempdir is not None
    assert not tempdir.exists()


def test_parser_construction_checks_paperless_compatibility(
    monkeypatch,
    paperless_version_module,
):
    monkeypatch.setattr(paperless_version_module, "__version__", (3, 2, 0))
    monkeypatch.setenv("PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS", "false")

    with pytest.raises(IncompatiblePaperlessError, match="3.2.0"):
        DoclingParser()


def test_parser_construction_honors_explicit_compatibility_override(
    monkeypatch,
    paperless_version_module,
):
    monkeypatch.setattr(paperless_version_module, "__version__", (3, 2, 0))
    monkeypatch.setenv("PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS", "true")

    with DoclingParser() as parser:
        assert parser.tempdir.is_dir()


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE"])
def test_parser_construction_rejects_ambiguous_compatibility_override(
    monkeypatch,
    value,
):
    monkeypatch.setenv("PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS", value)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    with pytest.raises(
        IncompatiblePaperlessError,
        match="must be either 'true' or 'false'",
    ) as error:
        DoclingParser()

    assert "must-not-leak" not in str(error.value)


def test_unimplemented_collaborator_methods_fail_explicitly(
    paperless_version_module,
):
    with DoclingParser() as parser:
        with pytest.raises(NotImplementedError):
            parser.parse(Path("document.pdf"), "application/pdf")
        with pytest.raises(NotImplementedError):
            parser.get_thumbnail(Path("document.pdf"), "application/pdf")
        with pytest.raises(NotImplementedError):
            parser.get_page_count(Path("document.pdf"), "application/pdf")
        with pytest.raises(NotImplementedError):
            parser.extract_metadata(Path("document.pdf"), "application/pdf")


@pytest.mark.parametrize("configured_version", [None, ""])
def test_install_script_requires_nonempty_package_version(configured_version):
    environment = os.environ.copy()
    if configured_version is None:
        environment.pop("PAPERLESS_DOCLING_VERSION", None)
    else:
        environment["PAPERLESS_DOCLING_VERSION"] = configured_version

    result = subprocess.run(
        [INSTALL_SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PAPERLESS_DOCLING_VERSION" in result.stderr


def test_install_script_installs_exact_version_without_dependencies(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_arguments = tmp_path / "python-arguments"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CAPTURED_ARGUMENTS"\n',
    )
    fake_python.chmod(0o755)
    environment = {
        **os.environ,
        "CAPTURED_ARGUMENTS": str(captured_arguments),
        "PAPERLESS_DOCLING_VERSION": "1.2.3",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    result = subprocess.run(
        [INSTALL_SCRIPT],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert captured_arguments.read_text().splitlines() == [
        "-m",
        "pip",
        "install",
        "--no-deps",
        "paperless-docling==1.2.3",
    ]


def test_built_wheel_contains_root_mit_license(tmp_path):
    result = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "paperless-docling",
            "--wheel",
            "--out-dir",
            str(tmp_path),
            "--no-create-gitignore",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheel = next(tmp_path.glob("paperless_docling-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        license_files = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/licenses/LICENSE")
        ]
        assert len(license_files) == 1
        assert archive.read(license_files[0]) == (PROJECT_ROOT / "LICENSE").read_bytes()
