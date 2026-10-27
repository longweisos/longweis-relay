#!/usr/bin/env python3
"""Build normalized, verified local release archives and checksums."""

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path, PurePosixPath
from typing import Dict, Sequence


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
SOURCE_DATE_EPOCH = 1793059200
SDIST_NAME = "longweis-relay-client-%s.tar.gz" % VERSION
WHEEL_NAME = "longweis_relay_client-%s-py3-none-any.whl" % VERSION
SDIST_ROOT = SDIST_NAME[: -len(".tar.gz")]
BUILD_TOOL_VERSIONS = {
    "setuptools": "79.0.1",
    "wheel": "0.48.0",
}


def _verify_build_tools() -> None:
    mismatches = []
    for package, expected in sorted(BUILD_TOOL_VERSIONS.items()):
        try:
            actual = distribution_version(package)
        except PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            mismatches.append("%s==%s (found %s)" % (package, expected, actual))
    if mismatches:
        raise RuntimeError(
            "release build requires the pinned tools: %s" % ", ".join(mismatches)
        )


def _single_artifact(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError("build produced an unexpected artifact set for %s" % pattern)
    return matches[0]


def _safe_name(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("build archive contains an unsafe path")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_mode(mode: int, directory: bool = False) -> int:
    if directory:
        return 0o755
    return 0o755 if mode & 0o111 else 0o644


def _normalize_sdist(source: Path, destination: Path) -> None:
    entries = []
    source_root = None
    with tarfile.open(source, "r:gz") as archive:
        for item in archive.getmembers():
            original = _safe_name(item.name)
            if source_root is None:
                source_root = original.parts[0]
            elif original.parts[0] != source_root:
                raise ValueError("source archive has multiple top-level roots")
            if len(original.parts) == 1 and not item.isdir():
                raise ValueError("source archive root is not a directory")
            normalized = PurePosixPath(SDIST_ROOT, *original.parts[1:])
            _safe_name(str(normalized))
            if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                raise ValueError("source archive contains a special file")
            if not (item.isfile() or item.isdir()):
                raise ValueError("source archive contains an unsupported entry")
            content = b""
            if item.isfile():
                extracted = archive.extractfile(item)
                if extracted is None:
                    raise ValueError("source archive entry is unreadable")
                content = extracted.read()
            entries.append((str(normalized), item.isdir(), item.mode, content))
    if source_root is None:
        raise ValueError("source archive is empty")
    with destination.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw,
            mtime=SOURCE_DATE_EPOCH,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, is_directory, source_mode, content in sorted(entries):
                    item = tarfile.TarInfo(name)
                    item.type = tarfile.DIRTYPE if is_directory else tarfile.REGTYPE
                    item.size = 0 if is_directory else len(content)
                    item.mode = _normalized_mode(source_mode, is_directory)
                    item.uid = 0
                    item.gid = 0
                    item.uname = ""
                    item.gname = ""
                    item.mtime = SOURCE_DATE_EPOCH
                    archive.addfile(item, None if is_directory else io.BytesIO(content))


def _normalize_wheel(source: Path, destination: Path) -> None:
    timestamp = datetime.fromtimestamp(SOURCE_DATE_EPOCH, timezone.utc)
    zip_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    entries = []
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            _safe_name(item.filename)
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or item.is_dir():
                if stat.S_ISLNK(mode):
                    raise ValueError("wheel contains a symbolic link")
                continue
            entries.append((item.filename, mode, archive.read(item)))
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, source_mode, content in sorted(entries):
            item = zipfile.ZipInfo(name, date_time=zip_time)
            item.create_system = 3
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = (_normalized_mode(source_mode) | stat.S_IFREG) << 16
            archive.writestr(item, content)


def build(output: Path) -> Dict[str, object]:
    _verify_build_tools()
    output = output.resolve()
    if output.exists():
        if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
            raise ValueError("release output must be a new or empty directory")
    else:
        output.mkdir(parents=True, mode=0o755)
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = str(SOURCE_DATE_EPOCH)
    with tempfile.TemporaryDirectory(prefix="relay-client-release-") as temporary:
        staging = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "setup.py",
                "sdist",
                "--dist-dir",
                str(staging),
                "bdist_wheel",
                "--dist-dir",
                str(staging),
            ],
            cwd=str(ROOT),
            env=environment,
            check=True,
        )
        # Setuptools normalizes the distribution name with either hyphens or
        # underscores depending on the installed release.  Accept exactly one
        # archive of each type, then publish our fixed canonical filenames.
        _normalize_sdist(
            _single_artifact(staging, "*.tar.gz"), output / SDIST_NAME
        )
        _normalize_wheel(_single_artifact(staging, "*.whl"), output / WHEEL_NAME)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_release.py"), str(output)],
        cwd=str(ROOT),
        check=True,
    )
    hashes = {
        name: _sha256(output / name) for name in (SDIST_NAME, WHEEL_NAME)
    }
    checksum_path = output / "SHA256SUMS"
    checksum_path.write_text(
        "".join("%s  %s\n" % (hashes[name], name) for name in sorted(hashes)),
        encoding="ascii",
    )
    shutil.copy2(ROOT / "docs" / "SBOM.spdx.json", output / "SBOM.spdx.json")
    return {
        "status": "built_and_verified",
        "version": VERSION,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "output": str(output),
        "artifacts": hashes,
    }


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Build Relay public-client release")
    parser.add_argument("--output", required=True, type=Path)
    parsed = parser.parse_args(arguments or None)
    print(json.dumps(build(parsed.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
