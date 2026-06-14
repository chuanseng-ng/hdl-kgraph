"""Parser backend interface.

Design (see ROADMAP.md, "Two-pass build architecture"):

* **Pass 1 (parse):** a :class:`ParserBackend` parses one file independently
  into a per-file IR — declared nodes plus unresolved references (instance
  targets, package imports, include paths). Pass 1 is parallelizable (the
  pipeline currently runs it serially; see issue #26) and is the only stage
  re-run for changed files during incremental updates.
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

#: Per-file cap on recorded parse-error *details*; ``parse_error_count``
#: stays exact beyond it (a garbage/minified file must not bloat the store).
MAX_PARSE_ERRORS = 20


class UnsupportedBackendError(NotImplementedError):
    """Raised by a registered-but-not-yet-implemented parser backend.

    Subclasses :class:`NotImplementedError` (so existing call sites keep
    working) but is a distinct, greppable type a future suffix router can
    catch to skip-with-warning instead of aborting the build. The stub
    backends (SDC/UPF/Tcl/Perl/SLN) are intentionally kept out of the
    discovery/pipeline routing path until implemented; this is the clean
    error they raise if one is ever dispatched to directly. See issue #77.
    """


def error_snippet(text: str, limit: int = 50) -> str:
    """First line of *text*, trimmed to *limit* chars, for error messages."""
    first, _, rest = text.strip().partition("\n")
    first = first.strip()
    if len(first) > limit or rest:
        return first[:limit].rstrip() + "..."
    return first


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
    ``DRIVES``/       PROCESS (or other site) /      ``role``: ``lhs``/``rhs``/
    ``READS``         signal name                    ``sensitivity``
    ``CLOCKED_BY``/   PROCESS, ASSERTION, COVER-     ``evidence``, ``edge``,
    ``RESETS``        GROUP, ... / clock or reset    ``is_async`` (RESETS)
    ``ASSERTS_ON``/   ASSERTION/PROPERTY/SEQUENCE/   --
    ``COVERS``        COVERPOINT / name in scope
    ================= ============================== ====================================

    Positional ``CONNECTS``/``PARAMETERIZES`` resolve against the target's
    PORT/PARAMETER children via their declaration-order ``attrs["index"]``.
    The M5 dataflow kinds (the last three rows) are **scoped**: ``target_name``
    resolves against the referring node's enclosing design unit's children,
    never globally — see :mod:`hdl_kgraph.graph.builder`. ``confidence`` on
    these carries the *evidence* score (1.0 sensitivity proof, 0.4 name
    heuristic), which the linker min-combines with resolution confidence.
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
    # Human-readable ``file:line: message`` details for the first
    # MAX_PARSE_ERRORS errors (so `build -v`/`status --errors` can point at
    # the offending source, not just count it).
    parse_errors: list[str] = field(default_factory=list)

    def record_parse_error(self, message: str) -> None:
        """Count one parse error, keeping the first MAX_PARSE_ERRORS details."""
        self.parse_error_count += 1
        if len(self.parse_errors) < MAX_PARSE_ERRORS:
            self.parse_errors.append(message)


class ParserBackend(Protocol):
    """A pass-1 parser for one or more languages."""

    #: File suffixes this backend handles (e.g. ``{".v", ".sv", ".svh"}``).
    suffixes: frozenset[str]

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one file into its per-file IR. Must tolerate syntax errors."""
        ...
