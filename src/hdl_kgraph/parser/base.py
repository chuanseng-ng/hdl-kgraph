"""Parser backend interface.

Design (see ROADMAP.md, "Two-pass build architecture"):

* **Pass 1 (parse):** a :class:`ParserBackend` parses one file independently
  into a per-file IR — declared nodes plus unresolved references (instance
  targets, package imports, include paths). Pass 1 is embarrassingly parallel
  and is the only stage re-run for changed files during incremental updates.
* **Pass 2 (link):** the graph builder resolves references across files with
  confidence scoring (see :mod:`hdl_kgraph.schema`).

Backends are intentionally swappable: the tree-sitter grammar choice (the #1
project risk) is isolated here, and M7 adds elaboration-accurate backends
(pyslang, GHDL/pyVHDLModel) behind the same interface with capability flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hdl_kgraph.schema import Edge, Node


@dataclass
class FileIR:
    """Pass-1 result for a single source file."""

    path: str
    nodes: list[Node] = field(default_factory=list)
    # Edges whose endpoints are both local to this file (e.g. DECLARES).
    local_edges: list[Edge] = field(default_factory=list)
    # References to be resolved in pass 2 (instance targets, imports, includes),
    # keyed by the referring node id. Shape is finalized in M1.
    unresolved_refs: dict[str, list[str]] = field(default_factory=dict)
    parse_error_count: int = 0


class ParserBackend(Protocol):
    """A pass-1 parser for one or more languages."""

    #: File suffixes this backend handles (e.g. ``{".v", ".sv", ".svh"}``).
    suffixes: frozenset[str]

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one file into its per-file IR. Must tolerate syntax errors."""
        ...
