import os
import subprocess
import sys
from pathlib import Path

import pytest
from paperless_docling import compatibility
from paperless_docling.compatibility import ensure_paperless_compatible
from paperless_docling.errors import IncompatiblePaperlessError

PACKAGE_SRC = Path(__file__).parents[3] / "packages" / "parser" / "src"


@pytest.mark.parametrize("running_version", ["3.1.0", "3.1.99"])
def test_accepts_supported_paperless_minor(monkeypatch, running_version):
    monkeypatch.setattr(compatibility, "paperless_version", lambda: running_version)

    ensure_paperless_compatible(allow_unsupported=False)


@pytest.mark.parametrize(
    "running_version",
    ["3.0.99", "3.1.0rc1", "3.2.0rc1", "3.2.0"],
)
def test_rejects_paperless_versions_outside_supported_minor(
    monkeypatch,
    running_version,
):
    monkeypatch.setattr(compatibility, "paperless_version", lambda: running_version)

    with pytest.raises(IncompatiblePaperlessError) as error:
        ensure_paperless_compatible(allow_unsupported=False)

    message = str(error.value)
    assert running_version in message
    assert ">=3.1,<3.2" in message
    assert "paperless-docling 0.1.0" in message


def test_override_allows_unsupported_paperless(monkeypatch):
    monkeypatch.setattr(compatibility, "paperless_version", lambda: "3.2.0")

    ensure_paperless_compatible(allow_unsupported=True)


def test_rejects_unparseable_paperless_version_as_incompatible(monkeypatch):
    monkeypatch.setattr(
        compatibility,
        "paperless_version",
        lambda: "not-a-version",
    )

    with pytest.raises(IncompatiblePaperlessError, match="not-a-version"):
        ensure_paperless_compatible(allow_unsupported=False)


def test_compatibility_module_imports_without_packaging():
    code = """
import builtins

real_import = builtins.__import__

def import_without_packaging(name, *args, **kwargs):
    if name == "packaging" or name.startswith("packaging."):
        raise ModuleNotFoundError("packaging is unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_packaging
import paperless_docling.compatibility
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(PACKAGE_SRC),
    }

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
