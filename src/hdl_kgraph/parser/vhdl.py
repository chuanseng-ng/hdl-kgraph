"""VHDL parser backend (M3).

Implementation notes:

* Candidate grammar: ``jpt13653903/tree-sitter-vhdl`` (maintained);
  ``alemuller/tree-sitter-vhdl`` is unmaintained.
* VHDL identifiers are case-insensitive: names are normalized to lowercase in
  this layer (not the grammar), with original casing preserved in
  ``Node.attrs``.
* Library/work mapping (``--lib work=./src`` style config) and
  component-vs-entity binding resolution happen in pass 2; this backend only
  records the references.
* M3 extracts ENTITY, ARCHITECTURE (+IMPLEMENTS), VHDL_PACKAGE/PACKAGE_BODY,
  CONFIGURATION (+BINDS), generics, ports, signals, processes, and both
  component and direct entity instantiation.
"""

from __future__ import annotations

from pathlib import Path

from hdl_kgraph.parser.base import FileIR

SUFFIXES = frozenset({".vhd", ".vhdl"})


class VhdlParser:
    """Tree-sitter based VHDL pass-1 parser. M3 work item."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("VHDL parsing lands in milestone M3")
