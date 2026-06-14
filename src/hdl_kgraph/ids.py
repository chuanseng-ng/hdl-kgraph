"""Stable node-id scheme.

Ids are human-readable and file-scoped, so an unrelated edit elsewhere in the
tree never changes them — the right granularity for M4's per-file incremental
rebuild. Paths are POSIX-style and relative to the build root.

* FILE nodes: ``file:{relpath}``
* FILELIST nodes: ``filelist:{relpath}``
* LIBRARY nodes: ``library:{name}`` (VHDL library names, lowercase)
* Declarations: ``{relpath}::{kind}:{dotted_scope_path}`` — e.g.
  ``rtl/fifo.sv::module:fifo``, ``rtl/fifo.sv::port:fifo.clk``,
  ``rtl/top.v::instance:top.u_counter``
* Duplicate names within one file are disambiguated with ``@{start_line}``.
* Unresolved stubs (created by the pass-2 linker, global so every referrer
  converges on the same node): ``unresolved:{kind}:{name}``, with stub ports
  ``unresolved:port:{module}.{port}``.
* Elaborated nodes (M7 semantic enrichment, keyed by their full elaborated
  hierarchical path so a generate loop's unrolled iterations get distinct,
  stable ids that never collide with the single syntactic instance):
  ``elab:{kind}:{hierarchical_path}`` — e.g.
  ``elab:instance:top.g_leaf[0].u_leaf`` (SystemVerilog ``[i]`` indexing; VHDL
  ``for ... generate`` uses ``(i)`` and seeds the path with ``entity(arch)`` so
  an entity's architectures never collide —
  ``elab:instance:gen_top(rtl).g_leaf(0).u_leaf``).
"""

from __future__ import annotations

from hdl_kgraph.schema import NodeKind


def file_node_id(relpath: str) -> str:
    """Id of the FILE node for *relpath*."""
    return f"file:{relpath}"


def filelist_node_id(relpath: str) -> str:
    """Id of the FILELIST node for *relpath*."""
    return f"filelist:{relpath}"


def library_node_id(name: str) -> str:
    """Id of the LIBRARY node for VHDL library *name* (already lowercase)."""
    return f"library:{name}"


def decl_node_id(relpath: str, kind: NodeKind, scope_path: str) -> str:
    """Id of a declaration at dotted *scope_path* (e.g. ``top.u_counter``)."""
    return f"{relpath}::{kind.value}:{scope_path}"


def stub_node_id(kind: NodeKind, name: str) -> str:
    """Id of an unresolved-stub node; shared by all referrers of *name*."""
    return f"unresolved:{kind.value}:{name}"


def elab_node_id(kind: NodeKind, hier_path: str) -> str:
    """Id of an elaboration-derived node (M7), keyed by hierarchical path."""
    return f"elab:{kind.value}:{hier_path}"


def parse_node_id(node_id: str) -> tuple[NodeKind, str] | None:
    """Recover (kind, name) from a node id, or None for an unknown shape.

    The linker uses this to materialize a typed stub for an edge endpoint
    that no parser ever emitted as a node, so the graph never carries the
    attribute-less nodes networkx would otherwise auto-create.
    """
    if "::" in node_id:  # declaration: {relpath}::{kind}:{dotted_scope}[@line[.row]]
        _, _, rest = node_id.partition("::")
        kind_text, sep, scope = rest.partition(":")
        if not sep:
            return None
        try:
            kind = NodeKind(kind_text)
        except ValueError:
            return None
        name = scope.split("@", 1)[0].rsplit(".", 1)[-1]
        return kind, name
    prefix, sep, rest = node_id.partition(":")
    if not sep:
        return None
    if prefix == "file":
        return NodeKind.FILE, rest.rsplit("/", 1)[-1]
    if prefix == "filelist":
        return NodeKind.FILELIST, rest.rsplit("/", 1)[-1]
    if prefix == "library":
        return NodeKind.LIBRARY, rest
    if prefix == "macro":
        return NodeKind.MACRO, rest.split("@", 1)[0]
    if prefix in ("unresolved", "elab"):
        kind_text, sep, name = rest.partition(":")
        if not sep:
            return None
        try:
            # An elaborated id's name is the last hierarchical-path segment.
            return NodeKind(kind_text), name.rsplit(".", 1)[-1] if prefix == "elab" else name
        except ValueError:
            return None
    return None
