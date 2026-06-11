"""Tcl parser backends — SDC/XDC/UPF constraints first, flow scripts second (M10).

Implementation notes:

* SDC, XDC, and UPF are constrained Tcl subsets, so all three backends share
  one command tokenizer and the ``get_ports``/``get_pins``/``get_cells``/
  ``get_clocks`` object-query parser. Queries resolve to design nodes in
  pass 2 (exact name 1.0; glob patterns 0.8 unique / 0.6 ambiguous).
* Phase 1a (SDC/XDC): ``create_clock``/``create_generated_clock`` -> CLOCK
  nodes (virtual and generated clocks supported); ``set_false_path``,
  ``set_multicycle_path``, ``set_input_delay``/``set_output_delay``,
  ``set_clock_groups`` -> TIMING_CONSTRAINT nodes with CONSTRAINS edges.
  ``create_clock`` is authoritative clock evidence: it upgrades M5's 0.4
  CLOCKED_BY heuristics to 1.0, and ``set_clock_groups -asynchronous`` /
  ``set_false_path`` feed the CDC report as declared-safe crossings.
* Phase 1b (UPF, IEEE 1801): ``create_power_domain`` -> POWER_DOMAIN nodes
  with CONSTRAINS edges to their elements; supply nets/sets and isolation/
  retention/level-shifter strategies recorded in attrs.
* Phase 2 (.tcl flow scripts): ``read_verilog``/``read_vhdl``/``analyze``/
  ``add_files`` -> REFERENCES_FILE edges; ``source`` chains -> INCLUDES.
  Only literal ``set`` variable substitution is attempted — Tcl is a full
  language and is never evaluated (see ROADMAP.md "Risks").
"""

from __future__ import annotations

from pathlib import Path

from hdl_kgraph.parser.base import FileIR

SDC_SUFFIXES = frozenset({".sdc", ".xdc"})
UPF_SUFFIXES = frozenset({".upf"})
SCRIPT_SUFFIXES = frozenset({".tcl"})


class SdcParser:
    """SDC/XDC timing-constraint pass-1 parser. M10 work item."""

    suffixes = SDC_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("SDC/XDC parsing lands in milestone M10")


class UpfParser:
    """UPF (IEEE 1801) power-intent pass-1 parser. M10 work item."""

    suffixes = UPF_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("UPF parsing lands in milestone M10")


class TclScriptParser:
    """Tool-flow Tcl script pass-1 scanner. M10 work item."""

    suffixes = SCRIPT_SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        raise NotImplementedError("Tcl flow-script scanning lands in milestone M10")
