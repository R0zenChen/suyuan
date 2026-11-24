"""Inspect a platform ZIP and run the bundled services from its extracted copy."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from package_release import PLATFORMS, ROOT


PLATFORM_ENTRY = {
    "windows-x64": "启动溯源.exe",
    "macos-arm64": "start-macos.command",
    "linux-x64": "start-linux.sh",
}
PRIVATE_NAMES = {".env", "mysql-config.json", "analysis-config.json", "accounts.json"}
PRIVATE_DIRS = {"data", "dist", "output", ".venv", "node_modules", "课程作业资料-不上传"}


def verify(platform: str, version: str) -> None:
    archive_path = ROOT / "dist" / f"Suyuan-v{version.removeprefix('v')}-{platform}.zip"
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        required = {"README.md", "使用说明书.md", "app.py", "requirements.txt",
                    "meltwater-studio/ui/server.mjs", "tools/smoke_release.py",
                    PLATFORM_ENTRY[platform]}
        missing = required - names
        if missing:
            raise AssertionError(f"发布包缺少文件：{sorted(missing)}")
        for name in names:
            parts = Path(name).parts
            if Path(name).name in PRIVATE_NAMES or PRIVATE_DIRS.intersection(parts):
                raise AssertionError(f"发布包包含私有路径：{name}")
            if name.startswith("/") or ".." in parts:
                raise AssertionError(f"发布包包含不安全路径：{name}")
        with tempfile.TemporaryDirectory(prefix="suyuan-release-") as tmp:
            archive.extractall(tmp)
            extracted = Path(tmp)
            subprocess.run([sys.executable, str(extracted / "tools/smoke_release.py"), str(extracted)],
                           cwd=extracted, check=True)
    print(f"Verified {archive_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--version", default=os.environ.get("RELEASE_VERSION", "0.1.0-dev"))
    options = parser.parse_args()
    verify(options.platform, options.version)
