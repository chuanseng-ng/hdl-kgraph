"""Graph builder: pass-2 linker (M1).

Consumes per-file IRs from the parser backends and produces the global
knowledge graph (NetworkX in memory, persisted via
:mod:`hdl_kgraph.storage.sqlite_store`).

Pass-2 responsibilities:

* instance -> definition resolution (module/entity name lookup across files)
* SV package import resolution; VHDL library/work scoping (M3)
* bind directives and VHDL configuration resolution (M3)
* cross-language Verilog<->VHDL linking (M3)
* confidence scoring per the convention in :mod:`hdl_kgraph.schema`;
  unresolved targets become stub nodes (``attrs["unresolved"] = True``)

Pass 2 is global but fast; incremental updates (M4) re-run pass 1 only for
changed files and then re-link.
"""
