#!/usr/bin/env python3
"""Verify public release archives without extracting or executing them."""

import argparse
import hashlib
import json
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, Sequence, Tuple


PACKAGE = "longweis-relay-client"
NORMALIZED = "longweis_relay_client"
VERSION = "0.1.0"
FORBIDDEN_SUFFIXES = frozenset(
    {".env", ".key", ".pem", ".p12", ".sqlite", ".sqlite3", ".db"}
)
FORBIDDEN_TEXT = (
    "project" + "_s",
    "activation" + "_code",
    "key" + "-claims",
    "claim" + "_token",
    "pyr" + "amid",
    "s-" + "core",
    "managed" + "_minimax",
    "private" + "_service",
    "call" + "_premium_review",
)
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".cfg", ".mjs", ".json", ".yml", ".yaml", ".txt", ".in"}
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> PurePosixPath:
    name = PurePosixPath(value)
    if name.is_absolute() or not name.parts or any(part in ("", ".", "..") for part in name.parts):
        raise ValueError("release archive contains an unsafe path")
    if name.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError("release archive contains a forbidden file type: %s" % name)
    return name


def _scan_text(name: PurePosixPath, content: bytes) -> None:
    if name.suffix.lower() not in TEXT_SUFFIXES:
        return
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("release text file is unexpectedly large: %s" % name)
    try:
        text = content.decode("utf-8").lower()
    except UnicodeDecodeError:
        raise ValueError("release text file is not UTF-8: %s" % name) from None
    if any(marker in text for marker in FORBIDDEN_TEXT):
        raise ValueError("release archive crosses the public boundary: %s" % name)


def _verify_wheel(path: Path) -> Mapping[str, object]:
    expected_prefix = NORMALIZED + "-" + VERSION + "-"
    if not path.name.startswith(expected_prefix) or path.suffix != ".whl":
        raise ValueError("wheel filename does not match the release version")
    names = []
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = _safe_name(item.filename)
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError("wheel contains a symbolic link")
            if item.is_dir():
                continue
            content = archive.read(item)
            _scan_text(name, content)
            names.append(str(name))
    required = {
        NORMALIZED + "/__init__.py",
        NORMALIZED + "/relay_memory_mode.mjs",
        NORMALIZED + "-" + VERSION + ".dist-info/METADATA",
        NORMALIZED + "-" + VERSION + ".dist-info/RECORD",
    }
    if not required.issubset(names):
        raise ValueError("wheel is missing required public-client files")
    if any("/tests/" in "/" + name or name.startswith("tests/") for name in names):
        raise ValueError("wheel unexpectedly includes tests")
    return {"file_count": len(names), "sha256": _sha256(path)}


def _verify_sdist(path: Path) -> Mapping[str, object]:
    expected_name = PACKAGE + "-" + VERSION + ".tar.gz"
    if path.name != expected_name:
        raise ValueError("source archive filename does not match the release version")
    root = PACKAGE + "-" + VERSION + "/"
    names = []
    with tarfile.open(path, "r:gz") as archive:
        for item in archive.getmembers():
            name = _safe_name(item.name)
            if item.issym() or item.islnk() or item.isdev() or item.isfifo():
                raise ValueError("source archive contains a special file")
            if item.isdir():
                continue
            if not item.isfile():
                raise ValueError("source archive contains an unsupported entry")
            extracted = archive.extractfile(item)
            if extracted is None:
                raise ValueError("source archive entry is unreadable")
            content = extracted.read()
            _scan_text(name, content)
            names.append(str(name))
    required = {
        root + "LICENSE",
        root + "README.md",
        root + "SECURITY.md",
        root + "CHANGELOG.md",
        root + "docs/PROTOCOL_V1.md",
        root + "docs/SBOM.spdx.json",
        root + "scripts/audit_public_tree.py",
        root + "scripts/verify_release.py",
        root + "src/longweis_relay_client/__init__.py",
        root + "src/longweis_relay_client/relay_memory_mode.mjs",
    }
    if not required.issubset(names):
        raise ValueError("source archive is missing required public files")
    return {"file_count": len(names), "sha256": _sha256(path)}


def verify(directory: Path) -> Dict[str, object]:
    wheel_candidates = sorted(directory.glob("*.whl"))
    source_candidates = sorted(directory.glob("*.tar.gz"))
    if len(wheel_candidates) != 1 or len(source_candidates) != 1:
        raise ValueError("release directory must contain one wheel and one source archive")
    wheel = wheel_candidates[0]
    source = source_candidates[0]
    return {
        "status": "verified",
        "package": PACKAGE,
        "version": VERSION,
        "artifacts": {
            wheel.name: _verify_wheel(wheel),
            source.name: _verify_sdist(source),
        },
    }


def main(arguments: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Verify Relay public release archives")
    parser.add_argument("directory", type=Path)
    parsed = parser.parse_args(arguments or None)
    result = verify(parsed.directory.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
