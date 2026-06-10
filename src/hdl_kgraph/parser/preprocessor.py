"""Lightweight SystemVerilog preprocessor (M2).

tree-sitter cannot expand macros, so heavily ``ifdef``'d code parses to
garbage and macro-defined module bodies are invisible without expansion. This
module is what separates a toy from real-world usability (ROADMAP.md risk #3).

Planned strategy:

* ``define`` with arguments, ``ifdef``/``ifndef``/``elsif`` branch
  selection from configured defines, and ``include`` resolution.
* Expansion produces a **line map** back to the original source so node spans
  stay accurate after substitution.
* "Both branches" mode: when no define set is configured, emit both sides of a
  conditional with ``CONFIDENCE_AMBIGUOUS``.
* Emits INCLUDES / DEFINES_MACRO / USES_MACRO edges and MACRO nodes.
"""
