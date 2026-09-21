import sys
from types import ModuleType

import pytest


@pytest.fixture
def paperless_version_module(monkeypatch):
    monkeypatch.delenv("PAPERLESS_DOCLING_ALLOW_UNSUPPORTED_PAPERLESS", raising=False)
    paperless = ModuleType("paperless")
    paperless.__path__ = []
    version_module = ModuleType("paperless.version")
    version_module.__dict__["__version__"] = (3, 1, 0)
    monkeypatch.setitem(sys.modules, "paperless", paperless)
    monkeypatch.setitem(sys.modules, "paperless.version", version_module)
    return version_module


@pytest.fixture
def paperless_parse_error(monkeypatch):
    class ParseError(Exception):
        pass

    documents = ModuleType("documents")
    documents.__path__ = []
    parsers = ModuleType("documents.parsers")
    parsers.__dict__["ParseError"] = ParseError
    monkeypatch.setitem(sys.modules, "documents", documents)
    monkeypatch.setitem(sys.modules, "documents.parsers", parsers)
    return ParseError
