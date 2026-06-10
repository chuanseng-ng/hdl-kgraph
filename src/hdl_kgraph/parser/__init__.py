"""Language parsers (pass 1 of the two-pass build).

Each backend turns one source file into a per-file IR of declarations and
unresolved references; cross-file resolution happens in
:mod:`hdl_kgraph.graph.builder`.
"""
