#!/usr/bin/env python3
"""Build the installable Minima-PiNet-Os DApp package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DAPP_ROOT = REPO_ROOT / "pinet_dapp"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "dist"
PACKAGE_PREFIX = "zedd-satellite-pinet-dapp"


def _iter_package_files(dapp_root: Path) -> Iterable[Path]:
    for path in sorted(dapp_root.rglob("*")):
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(dapp_root).parts):
            yield path


def _load_manifest(dapp_root: Path) -> dict:
    manifest_path = dapp_root / "dapp.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    required = ("id", "name", "version", "kind", "entryPoint", "permissions")
    missing = [field for field in required if not manifest.get(field)]
    if missing:
        raise ValueError(f"DApp manifest missing required fields: {', '.join(missing)}")

    entry = dapp_root / str(manifest["entryPoint"])
    if not entry.is_file():
        raise FileNotFoundError(f"DApp entryPoint does not exist: {entry}")

    return manifest


def build_package(dapp_root: Path = DEFAULT_DAPP_ROOT, output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    dapp_root = Path(dapp_root).resolve()
    output_dir = Path(output_dir).resolve()
    manifest = _load_manifest(dapp_root)
    version = str(manifest["version"])
    package_path = output_dir / f"{PACKAGE_PREFIX}-{version}.zip"
    checksum_path = package_path.with_suffix(package_path.suffix + ".sha256")

    output_dir.mkdir(parents=True, exist_ok=True)
    if package_path.exists():
        package_path.unlink()
    if checksum_path.exists():
        checksum_path.unlink()

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _iter_package_files(dapp_root):
            archive.write(path, path.relative_to(dapp_root).as_posix())

    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {package_path.name}{os.linesep}", encoding="utf-8")
    return package_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dapp-root", type=Path, default=DEFAULT_DAPP_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    package_path = build_package(args.dapp_root, args.output_dir)
    print(package_path)


if __name__ == "__main__":
    main()
