from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


APP_NAME = "Background Screen Controller"
APP_EXE_NAME = "Background Screen Controller.exe"
INSTALLER_PREFIX = "BackgroundScreenControllerSetup"
ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
PYINSTALLER_VERSION_FILE = ROOT / "file_version_info.txt"


def read_version() -> str:
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SystemExit(f"Unable to read VERSION file: {error}") from error
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", value):
        raise SystemExit("VERSION must look like 1.0.0 or 1.0.0.0.")
    return value


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def write_pyinstaller_version_file(version: str) -> Path:
    major, minor, patch, build = version_tuple(version)
    numbers = f"{major}, {minor}, {patch}, {build}"
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({numbers}),
    prodvers=({numbers}),
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Cosoro'),
          StringStruct('FileDescription', '{APP_NAME}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', '{APP_NAME}'),
          StringStruct('OriginalFilename', '{APP_EXE_NAME}'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    PYINSTALLER_VERSION_FILE.write_text(content, encoding="utf-8")
    return PYINSTALLER_VERSION_FILE


def sha256_for_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_update_manifest(version: str, installer_path: Path, output_path: Path) -> Path:
    if not installer_path.exists():
        raise SystemExit(f"Installer not found: {installer_path}")
    payload = {
        "appName": APP_NAME,
        "version": version,
        "releasedAtUtc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "installer": {
            "fileName": installer_path.name,
            "relativeUrl": installer_path.name,
            "sha256": sha256_for_file(installer_path),
            "sizeBytes": installer_path.stat().st_size,
        },
        "notes": "",
        "minimumSupportedVersion": version,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release helpers.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("print-version")
    subparsers.add_parser("prepare-build")
    manifest_parser = subparsers.add_parser("write-update-manifest")
    manifest_parser.add_argument("--installer", required=True)
    manifest_parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    version = read_version()
    if args.command == "print-version":
        print(version)
        return 0
    if args.command == "prepare-build":
        print(write_pyinstaller_version_file(version))
        return 0
    if args.command == "write-update-manifest":
        print(write_update_manifest(version, Path(args.installer), Path(args.output)))
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
