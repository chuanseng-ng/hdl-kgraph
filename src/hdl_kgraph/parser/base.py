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
from typing import Any, Protocol

from hdl_kgraph.schema import CONFIDENCE_RESOLVED, Edge, EdgeKind, Node


@dataclass
class UnresolvedRef:
    """A cross-file reference recorded in pass 1 and resolved in pass 2.

    ``attrs`` carries the per-kind payload the linker needs:

    ================= ============================== ====================================
    ``edge_kind``     ``src_id`` / ``target_name``   ``attrs``
    ================= ============================== ====================================
    ``INSTANTIATES``  INSTANCE node / target module  --
    ``CONNECTS``      INSTANCE node / target module  ``port_name: str | None``,
      (per binding)                                  ``position: int | None``,
                                                     ``wildcard: bool``, ``expr_text: str``
    ``PARAMETERIZES`` INSTANCE node / target module  ``param_name: str | None``,
      (per override)                                 ``position: int | None``,
                                                     ``value_text: str``
    ``IMPORTS``       importing scope / package      ``symbol``: ``"*"`` or explicit name
    ``EXTENDS``       CLASS node / base class        ``package: str | None``,
                                                     ``param_args_text: str | None``
    ================= ============================== ====================================

    Positional ``CONNECTS``/``PARAMETERIZES`` resolve against the target's
    PORT/PARAMETER children via their declaration-order ``attrs["index"]``.
    """

    edge_kind: EdgeKind
    src_id: str  # id of the referring node (present in FileIR.nodes)
    target_name: str  # bare name to resolve (module/package/class name)
    line_span: tuple[int, int] = (0, 0)
    attrs: dict[str, Any] = field(default_factory=dict)
    # Confidence of the reference *site* itself; below 1.0 for references in
    # non-selected both-branches preprocessor regions. The linker emits
    # min(resolution confidence, this).
    confidence: float = CONFIDENCE_RESOLVED


@dataclass
class FileIR:
    """Pass-1 result for a single source file."""

    path: str
    nodes: list[Node] = field(default_factory=list)
    # Edges whose endpoints are both local to this file (e.g. DECLARES).
    local_edges: list[Edge] = field(default_factory=list)
    # References to be resolved in pass 2 (instance targets, imports, extends).
    unresolved_refs: list[UnresolvedRef] = field(default_factory=list)
    parse_error_count: int = 0


class ParserBackend(Protocol):
    """A pass-1 parser for one or more languages."""

    #: File suffixes this backend handles (e.g. ``{".v", ".sv", ".svh"}``).
    suffixes: frozenset[str]

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one file into its per-file IR. Must tolerate syntax errors."""
        ...
