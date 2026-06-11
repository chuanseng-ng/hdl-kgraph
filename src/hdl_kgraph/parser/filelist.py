"""Filelist (``.f`` / ``.vc``) parser (M2).

Supported syntax, matching common simulator conventions:

* one source path per token, comments (``//``, ``#``) to end of line
* ``+incdir+<dir>[+<dir>...]`` and ``+define+<NAME>[=<value>][+...]``
* nested filelists via ``-f <file>`` (cycles detected, never raised)
* ``-y <dir>`` library dirs (recorded only in M2) and ``-v <file>`` library
  files (compiled after all regular sources, simulator-style)
* environment-variable expansion (``$VAR`` / ``${VAR}``); unset variables
  leave the token untouched and record a warning

Unknown ``-flag`` / ``+plusarg+`` tokens are skipped with a warning — vendor
filelists carry simulator options we must tolerate. Relative paths resolve
against the filelist's own directory, so nested ``-f`` in other directories
just works. File order is preserved (and persisted on REFERENCES_FILE edge
``attrs["order"]``) because compile order governs ``define`` visibility.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hdl_kgraph.config import parse_define
from hdl_kgraph.ids import file_node_id, filelist_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.schema import Edge, EdgeKind, Node, NodeKind

FILELIST_SUFFIXES = frozenset({".f", ".vc"})

_ENV_VAR = re.compile(r"\$(\w+)|\$\{([^}]*)\}")
# Flags that consume the following token; -f/-y/-v are handled explicitly.
_ARG_FLAGS = frozenset({"-f", "-y", "-v"})


@dataclass
class Filelist:
    """One parsed filelist; ``entries`` preserves global compile order."""

    path: Path  # absolute, resolved
    entries: list[Path | Filelist] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    library_dirs: list[Path] = field(default_factory=list)  # -y (recorded only in M2)
    library_files: list[Path] = field(default_factory=list)  # -v
    warnings: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        """Source files listed directly in this filelist, in order."""
        return [entry for entry in self.entries if isinstance(entry, Path)]

    @property
    def nested(self) -> list[Filelist]:
        """Nested ``-f`` filelists, in order of appearance."""
        return [entry for entry in self.entries if isinstance(entry, Filelist)]


def parse_filelist(
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    _stack: tuple[Path, ...] = (),
) -> Filelist:
    """Parse *path* recursively; problems become warnings, never exceptions."""
    path = path.resolve()
    env_map = os.environ if env is None else env
    fl = Filelist(path=path)
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        fl.warnings.append(f"cannot read {path}: {exc}")
        return fl

    base = path.parent
    tokens = list(_tokenize(text))
    i = 0
    while i < len(tokens):
        token, expand_warnings = _expand_env(tokens[i], env_map)
        fl.warnings.extend(f"{path.name}: {w}" for w in expand_warnings)
        i += 1
        if token in _ARG_FLAGS:
            if i >= len(tokens):
                fl.warnings.append(f"{path.name}: {token} at end of file ignored")
                continue
            arg, expand_warnings = _expand_env(tokens[i], env_map)
            fl.warnings.extend(f"{path.name}: {w}" for w in expand_warnings)
            i += 1
            target = (base / arg).resolve()
            if token == "-f":
                if target in (*_stack, path):
                    fl.warnings.append(f"{path.name}: filelist cycle via {arg} skipped")
                else:
                    fl.entries.append(parse_filelist(target, env=env_map, _stack=(*_stack, path)))
            elif token == "-y":
                fl.library_dirs.append(target)
            else:  # -v
                fl.library_files.append(target)
        elif token.startswith("+incdir+"):
            dirs = token[len("+incdir+") :].split("+")
            fl.incdirs.extend((base / d).resolve() for d in dirs if d)
        elif token.startswith("+define+"):
            for item in token[len("+define+") :].split("+"):
                if item:
                    name, value = parse_define(item)
                    fl.defines[name] = value
        elif token.startswith(("-", "+")):
            fl.warnings.append(f"{path.name}: unknown option {token!r} skipped")
        else:
            fl.entries.append((base / token).resolve())
    return fl


def _tokenize(text: str) -> Iterator[str]:
    for line in text.splitlines():
        for marker in ("//", "#"):
            pos = line.find(marker)
            if pos != -1:
                line = line[:pos]
        yield from line.split()


def _expand_env(token: str, env: Mapping[str, str]) -> tuple[str, list[str]]:
    warnings: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name in env:
            return env[name]
        warnings.append(f"undefined environment variable ${name} left as-is")
        return match.group(0)

    return _ENV_VAR.sub(replace, token), warnings


# -- flattening helpers (global compile order) ---------------------------------


def flattened_files(fl: Filelist) -> list[Path]:
    """All source files in compile order; duplicates keep their first position.

    ``-v`` library files compile after all regular sources, simulator-style.
    """
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in [*_walk_files(fl), *_walk_library_files(fl)]:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _walk_files(fl: Filelist) -> Iterator[Path]:
    for entry in fl.entries:
        if isinstance(entry, Filelist):
            yield from _walk_files(entry)
        else:
            yield entry


def _walk_library_files(fl: Filelist) -> Iterator[Path]:
    yield from fl.library_files
    for nested in fl.nested:
        yield from _walk_library_files(nested)


def flattened_incdirs(fl: Filelist) -> list[Path]:
    """Include dirs in search order (this list's first, then nested in order)."""
    ordered: list[Path] = []
    for directory in [*fl.incdirs, *(d for n in fl.nested for d in flattened_incdirs(n))]:
        if directory not in ordered:
            ordered.append(directory)
    return ordered


def flattened_defines(fl: Filelist) -> dict[str, str | None]:
    """Defines merged across nesting; later occurrences override the value."""
    defines = dict(fl.defines)
    for nested in fl.nested:
        defines.update(flattened_defines(nested))
    return defines


def flattened_warnings(fl: Filelist) -> list[str]:
    warnings = list(fl.warnings)
    for nested in fl.nested:
        warnings.extend(flattened_warnings(nested))
    return warnings


# -- graph emission -------------------------------------------------------------


def filelist_irs(fl: Filelist, base: Path) -> list[FileIR]:
    """FILELIST nodes and edges for *fl* and its nested filelists.

    Each filelist becomes a FILELIST node with REFERENCES_FILE edges to its
    direct sources and INCLUDES edges to nested filelists, both carrying
    ``attrs["order"]`` (position among the list's entries) so global compile
    order is recoverable from the graph. Minimal FILE nodes are emitted for
    referenced sources so the graph never dangles; the linker keeps the
    parser's richer FILE node when the file is also parsed.
    """
    irs: list[FileIR] = []
    seen: set[Path] = set()
    _emit(fl, base, irs, seen)
    return irs


def _emit(fl: Filelist, base: Path, irs: list[FileIR], seen: set[Path]) -> None:
    if fl.path in seen:
        return
    seen.add(fl.path)
    relpath = _relpath(fl.path, base)
    ir = FileIR(path=relpath)
    node_id = filelist_node_id(relpath)
    ir.nodes.append(
        Node(
            id=node_id,
            kind=NodeKind.FILELIST,
            name=fl.path.name,
            qualified_name=relpath,
            file=relpath,
            attrs={
                "incdirs": [_relpath(d, base) for d in fl.incdirs],
                "defines": dict(fl.defines),
                "library_dirs": [_relpath(d, base) for d in fl.library_dirs],
                "warnings": list(fl.warnings),
            },
        )
    )
    for order, entry in enumerate(fl.entries):
        if isinstance(entry, Filelist):
            ir.local_edges.append(
                Edge(
                    src=node_id,
                    dst=filelist_node_id(_relpath(entry.path, base)),
                    kind=EdgeKind.INCLUDES,
                    attrs={"order": order},
                )
            )
        else:
            file_rel = _relpath(entry, base)
            ir.nodes.append(_file_stub(entry, file_rel))
            ir.local_edges.append(
                Edge(
                    src=node_id,
                    dst=file_node_id(file_rel),
                    kind=EdgeKind.REFERENCES_FILE,
                    attrs={"order": order, "role": "compile"},
                )
            )
    for entry in fl.library_files:
        file_rel = _relpath(entry, base)
        ir.nodes.append(_file_stub(entry, file_rel))
        ir.local_edges.append(
            Edge(
                src=node_id,
                dst=file_node_id(file_rel),
                kind=EdgeKind.REFERENCES_FILE,
                attrs={"role": "library"},
            )
        )
    irs.append(ir)
    for nested in fl.nested:
        _emit(nested, base, irs, seen)


def _file_stub(path: Path, relpath: str) -> Node:
    return Node(
        id=file_node_id(relpath),
        kind=NodeKind.FILE,
        name=path.name,
        qualified_name=relpath,
        file=relpath,
    )


def _relpath(path: Path, base: Path) -> str:
    """POSIX path relative to *base*; may contain ``..`` for out-of-root files."""
    return Path(os.path.relpath(path, base)).as_posix()
