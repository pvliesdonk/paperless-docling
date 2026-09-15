import configparser
import os
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[3]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "paperless-docling",
            "--wheel",
            "--out-dir",
            str(output_dir),
            "--no-create-gitignore",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return next(output_dir.glob("paperless_docling-*.whl"))


def test_built_wheel_declares_runtime_contract_without_dependencies(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_path))

    assert set(metadata["Requires-Python"].split(",")) == {">=3.14", "<3.15"}
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("Requires-Dist") is None


def test_built_wheel_declares_exact_parser_entry_point(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        entry_points_path = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        )
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points_path).decode())

    assert dict(parser["paperless_ngx.parsers"]) == {
        "docling": "paperless_docling.parser:DoclingParser",
    }
    assert parser.sections() == ["paperless_ngx.parsers"]


def test_wheel_installs_and_loads_parser_entry_point_on_python_3_14(
    built_wheel,
    tmp_path,
):
    assert sys.version_info[:2] == (3, 14)
    install_target = tmp_path / "installed"
    install_result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(install_target),
            "--no-deps",
            "--no-index",
            str(built_wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install_result.returncode == 0, install_result.stderr

    protocol_root = tmp_path / "protocol"
    paperless_package = protocol_root / "paperless"
    paperless_package.mkdir(parents=True)
    (paperless_package / "__init__.py").write_text("")
    (paperless_package / "version.py").write_text("__version__ = (3, 1, 0)\n")
    (paperless_package / "parsers.py").write_text(
        "class MetadataEntry: pass\nclass ParserContext: pass\n",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(install_target), str(protocol_root)]),
    }
    load_result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            """
import sys
from importlib.metadata import distribution

assert sys.version_info[:2] == (3, 14)
dist = distribution("paperless-docling")
entry_points = [
    entry_point
    for entry_point in dist.entry_points
    if entry_point.group == "paperless_ngx.parsers"
]
assert [(entry_point.name, entry_point.value) for entry_point in entry_points] == [
    ("docling", "paperless_docling.parser:DoclingParser"),
]
parser_class = entry_points[0].load()
with parser_class() as parser:
    assert parser.name == "Paperless Docling Parser"
    assert parser.tempdir.is_dir()
""",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert load_result.returncode == 0, load_result.stderr
