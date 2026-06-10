"""Source-file discovery with the M1 real-world-input guards.

Walks the build root for files matching the registered parser suffixes and
skips — while still recording, so ``status`` can report them — files that
match an exclude glob, exceed the size guard (huge generated netlists), or
contain ``\\`pragma protect`` encrypted IP (ROADMAP Risk #7). Filelists and
``hdl-kgraph.toml`` configuration arrive in M2; M1 exposes the two knobs as
CLI flags.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path

from hdl_kgraph.parser.systemverilog import SUFFIXES, SYSTEMVERILOG_SUFFIXES
from hdl_kgraph.schema import Language

DEFAULT_MAX_FILE_SIZE_KB = 1024
_PRAGMA_PROTECT_PROBE_BYTES = 4096


@dataclass
class DiscoveredFile:
    """One candidate source file, possibly skipped by a guard."""

    path: Path  # absolute
    relpath: str  # POSIX, relative to the build root
    language: Language
    size_bytes: int
    content_hash: str = ""
    skipped_reason: str | None = None  # None | 'exclude' | 'size' | 'pragma_protect'


def _language_for(path: Path) -> Language:
    return Language.SYSTEMVERILOG if path.suffix in SYSTEMVERILOG_SUFFIXES else Language.VERILOG


def discover(
    root: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> list[DiscoveredFile]:
    """Find SV/Verilog sources under *root* (or just *root* if it is a file)."""
    root = root.resolve()
    if root.is_file():
        paths = [root] if root.suffix in SUFFIXES else []
        base = root.parent
    else:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES)
        base = root

    results: list[DiscoveredFile] = []
    for path in paths:
        relpath = path.relative_to(base).as_posix()
        size = path.stat().st_size
        found = DiscoveredFile(
            path=path, relpath=relpath, language=_language_for(path), size_bytes=size
        )
        if any(fnmatch.fnmatch(relpath, pattern) for pattern in exclude):
            found.skipped_reason = "exclude"
        elif size > max_file_size_kb * 1024:
            found.skipped_reason = "size"
        else:
            head = path.open("rb").read(_PRAGMA_PROTECT_PROBE_BYTES)
            if b"`pragma protect" in head:
                found.skipped_reason = "pragma_protect"
            else:
                found.content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        results.append(found)
    return results
