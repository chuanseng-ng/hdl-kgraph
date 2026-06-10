"""SystemVerilog / Verilog parser backend (M1).

Implementation notes:

* One tree-sitter grammar serves both ``.v`` and ``.sv``. The first M1 task is
  a grammar bake-off: ``gmlarumbe/tree-sitter-systemverilog`` (actively
  maintained, validated against sv-tests) vs the stale official
  ``tree-sitter/tree-sitter-verilog``, run against the fixture corpus.
* M1 extracts MODULE, INTERFACE, PACKAGE, PROGRAM, FUNCTION/TASK, PORT,
  PARAMETER, INSTANCE, TYPEDEF/STRUCT/ENUM, and CLASS (declaration + EXTENDS),
  with DECLARES / INSTANTIATES / CONNECTS / PARAMETERIZES / IMPORTS / EXTENDS
  edges.
* Files containing tree-sitter ERROR nodes must still yield partial results;
  the error count is reported in ``FileIR.parse_error_count``.
"""

from __future__ import annotations

from pathlib import Path

from hdl_kgraph.parser.base import FileIR

SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})


class SystemVerilogParser:
    """Tree-sitter based SystemVerilog/Verilog pass-1 parser. M1 work item."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("SystemVerilog parsing lands in milestone M1")
