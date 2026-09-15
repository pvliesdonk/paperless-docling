import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[3]


@pytest.fixture(scope="module")
def built_worker_wheel(tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("worker-wheel")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "paperless-docling-worker",
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
    return next(output_dir.glob("paperless_docling_worker-*.whl"))


def test_worker_wheel_contains_mit_license_notice(built_worker_wheel):
    with zipfile.ZipFile(built_worker_wheel) as archive:
        license_paths = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/licenses/LICENSE")
        ]
        assert len(license_paths) == 1
        notice = archive.read(license_paths[0]).decode()

    assert notice.startswith("MIT License\n\nCopyright (c) 2026 Peter van Liesdonk")
    assert "Permission is hereby granted, free of charge" in notice
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in notice
