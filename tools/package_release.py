"""Build source-based platform release archives without local secrets or data."""

from __future__ import annotations

import argparse
import os
import re
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"windows-x64", "macos-arm64", "linux-x64"}
ROOT_FILES = [
    "README.md", "使用说明书.md", "DEVELOPMENT.md", "requirements.txt", ".env.example",
    ".github/workflows/release.yml", "tools/package_release.py", "tools/smoke_release.py",
    "tools/verify_release.py",
]
SOURCE_DIRS = ["static", "browser-extension", "meltwater-studio", "launcher", "tests", "docs"]
EXCLUDED_PARTS = {"data", "output", "node_modules", "__pycache__", ".venv", ".git", "tmp",
                  "课程作业资料-不上传"}


def files_for(platform: str) -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    files.extend(ROOT.glob("*.py"))
    for dirname in SOURCE_DIRS:
        files.extend(p for p in (ROOT / dirname).rglob("*")
                     if p.is_file() and not p.is_symlink()
                     and not EXCLUDED_PARTS.intersection(p.relative_to(ROOT).parts))
    if platform == "windows-x64":
        files.extend(ROOT / name for name in ("启动溯源.exe", "启动演示.cmd", "install-windows.cmd"))
    else:
        files.extend(ROOT / name for name in ("launchers/start-unix.sh",
                                                  "start-macos.command" if platform == "macos-arm64" else "start-linux.sh"))
    missing = [str(p.relative_to(ROOT)) for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError("发布包缺少文件：" + ", ".join(missing))
    return sorted(set(files), key=lambda p: p.relative_to(ROOT).as_posix())


def build(platform: str, version: str) -> Path:
    version = version.removeprefix("v")
    if platform not in PLATFORMS or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-.][A-Za-z0-9.]+)?", version):
        raise ValueError("平台或版本号无效")
    target = ROOT / "dist" / f"Suyuan-v{version}-{platform}.zip"
    target.parent.mkdir(exist_ok=True)
    files = files_for(platform)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in files:
            name = file.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith((".sh", ".command")) else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, file.read_bytes())
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert "app.py" in names and "meltwater-studio/ui/server.mjs" in names
        assert "使用说明书.md" in names
        assert not any(EXCLUDED_PARTS.intersection(Path(name).parts) for name in names)
        assert not any(name.endswith((".env", "mysql-config.json", "accounts.json", ".jsonl")) for name in names)
        assert not any(any(marker in name for marker in ("产品设计说明书", "大作业任务书", "评分标准详解",
                                                       "模板-产品设计说明书", "答辩PPT", "领域方案")) for name in names)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--version", default=os.environ.get("RELEASE_VERSION", "0.1.0-dev"))
    args = parser.parse_args()
    result = build(args.platform, args.version)
    print(f"{result} ({result.stat().st_size:,} bytes)")
