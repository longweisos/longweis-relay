#!/usr/bin/env python3
"""Fail when the public tree contains known private implementation markers."""

from pathlib import Path
from typing import Iterable, Tuple


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".cfg", ".mjs", ".json", ".yml", ".yaml", ".txt", ".in"}
)
IGNORED_PARTS = frozenset(
    {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache"}
)
FORBIDDEN = (
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


def source_files() -> Iterable[Path]:
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            yield path


def findings() -> Tuple[str, ...]:
    results = []
    for path in source_files():
        text = path.read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN:
            if marker in text:
                results.append("%s: forbidden private marker" % path.relative_to(ROOT))
    return tuple(results)


def main() -> int:
    failures = findings()
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("public-tree audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
