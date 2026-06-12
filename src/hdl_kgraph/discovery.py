"""Source-file discovery with the M1 real-world-input guards.

Walks the build root (following directory symlinks, with loop protection)
for files matching the registered parser suffixes and
skips — while still recording, so ``status`` can report them — files that
match an exclude glob, exceed the size guard (huge generated netlists), or
contain ``\\`pragma protect`` encrypted IP (ROADMAP Risk #7).

M2 adds :func:`discover_from_paths` for explicit file sets (filelists,
config source globs): input order is preserved because compile order governs
``\\`define`` visibility, and two extra skip reasons appear — ``missing``
(a filelist entry that does not exist) and ``unsupported`` (a suffix no
parser handles yet). M3 adds the VHDL suffixes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hdl_kgraph.parser.systemverilog import SUFFIXES as SV_SUFFIXES
from hdl_kgraph.parser.systemverilog import SYSTEMVERILOG_SUFFIXES
from hdl_kgraph.parser.vhdl import SUFFIXES as VHDL_SUFFIXES
from hdl_kgraph.schema import Language

SUFFIXES = SV_SUFFIXES | VHDL_SUFFIXES

DEFAULT_MAX_FILE_SIZE_KB = 1024
_PRAGMA_PROTECT_PROBE_BYTES = 4096


@dataclass
class DiscoveredFile:
    """One candidate source file, possibly skipped by a guard."""

    path: Path  # absolute
    relpath: str  # POSIX, relative to the build root (may contain ``..``)
    language: Language
    size_bytes: int
    content_hash: str = ""
    # None | 'exclude' | 'size' | 'pragma_protect' | 'missing' | 'unsupported'
    skipped_reason: str | None = None


def _language_for(path: Path) -> Language:
    if path.suffix in VHDL_SUFFIXES:
        return Language.VHDL
    if path.suffix not in SV_SUFFIXES:
        return Language.UNKNOWN
    return Language.SYSTEMVERILOG if path.suffix in SYSTEMVERILOG_SUFFIXES else Language.VERILOG


def check_file(
    path: Path,
    base: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> DiscoveredFile:
    """Apply the skip guards to one file (already resolved to absolute)."""
    relpath = Path(os.path.relpath(path, base)).as_posix()
    found = DiscoveredFile(path=path, relpath=relpath, language=_language_for(path), size_bytes=0)
    if not path.is_file():
        found.skipped_reason = "missing"
        return found
    found.size_bytes = path.stat().st_size
    if path.suffix not in SUFFIXES:
        found.skipped_reason = "unsupported"
    elif any(fnmatch.fnmatch(relpath, pattern) for pattern in exclude):
        found.skipped_reason = "exclude"
    elif found.size_bytes > max_file_size_kb * 1024:
        found.skipped_reason = "size"
    else:
        # One read serves both the pragma-protect probe and the hash; the
        # size guard above already bounds how much this loads.
        data = path.read_bytes()
        if b"`pragma protect" in data[:_PRAGMA_PROTECT_PROBE_BYTES]:
            found.skipped_reason = "pragma_protect"
        else:
            found.content_hash = hashlib.sha256(data).hexdigest()
    return found


def _walk_sources(root: Path) -> Iterator[Path]:
    """Yield files with parser suffixes under *root*, following dir symlinks.

    ``Path.rglob`` does not descend into symlinked directories (until the
    Python 3.13 ``recurse_symlinks`` flag), so vendor/IP trees linked into
    the build root were silently missed. Visited real paths are tracked to
    break symlink loops.
    """
    visited = {root.resolve()}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        kept = []
        for name in sorted(dirnames):
            real = (Path(dirpath) / name).resolve()
            if real not in visited:
                visited.add(real)
                kept.append(name)
        dirnames[:] = kept  # prune already-visited (loop) dirs in place
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in SUFFIXES and path.is_file():
                yield path


def discover(
    root: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> list[DiscoveredFile]:
    """Find SV/Verilog sources under *root* (or just *root* if it is a file).

    Directory symlinks are followed; a file reachable through more than one
    path (symlink alias or loop) is reported once, under its first path in
    sorted order.
    """
    root = root.resolve()
    if root.is_file():
        paths = [root] if root.suffix in SUFFIXES else []
        base = root.parent
    else:
        paths = []
        seen: set[Path] = set()
        for path in sorted(_walk_sources(root)):
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                paths.append(path)
        base = root
    return [check_file(path, base, exclude, max_file_size_kb) for path in paths]


def discover_from_paths(
    paths: Iterable[Path],
    base: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> list[DiscoveredFile]:
    """Apply the guards to an explicit file set, preserving input order.

    Duplicates (same resolved path) keep their first position only, matching
    how simulators compile a file once.
    """
    seen: set[Path] = set()
    results: list[DiscoveredFile] = []
    for path in paths:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        results.append(check_file(path, base, exclude, max_file_size_kb))
    return results
