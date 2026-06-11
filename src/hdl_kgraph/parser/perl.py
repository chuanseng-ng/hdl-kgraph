"""Perl parser backend (M10).

Implementation notes:

* Scope is legacy EDA codegen scripts, not Perl semantics: detect which HDL
  files a script reads/writes/generates and record the lineage.
* ``open()`` calls whose path literal ends in an HDL suffix (``.v``/``.sv``/
  ``.vhd``/...) -> REFERENCES_FILE edges with ``attrs["mode"]`` =
  ``read``/``write``; heredoc bodies that look like Verilog (``module``...
  ``endmodule``) mark the script as a generator.
* Generated RTL links back to its generator via GENERATED_FROM (same edge
  M9 uses for Chisel/Amaranth/SpinalHDL output).
* First implementation is a line/regex scan; ``tree-sitter-perl`` exists if
  that proves insufficient. Expectations are modest by design (see
  ROADMAP.md "Risks").
"""

from __future__ import annotations

from pathlib import Path

from hdl_kgraph.parser.base import FileIR

SUFFIXES = frozenset({".pl", ".pm"})


class PerlParser:
    """Perl codegen-lineage pass-1 scanner. M10 work item."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("Perl codegen scanning lands in milestone M10")
