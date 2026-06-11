"""Stable node-id scheme.

Ids are human-readable and file-scoped, so an unrelated edit elsewhere in the
tree never changes them — the right granularity for M4's per-file incremental
rebuild. Paths are POSIX-style and relative to the build root.

* FILE nodes: ``file:{relpath}``
* FILELIST nodes: ``filelist:{relpath}``
* Declarations: ``{relpath}::{kind}:{dotted_scope_path}`` — e.g.
  ``rtl/fifo.sv::module:fifo``, ``rtl/fifo.sv::port:fifo.clk``,
  ``rtl/top.v::instance:top.u_counter``
* Duplicate names within one file are disambiguated with ``@{start_line}``.
* Unresolved stubs (created by the pass-2 linker, global so every referrer
  converges on the same node): ``unresolved:{kind}:{name}``, with stub ports
  ``unresolved:port:{module}.{port}``.
"""

from __future__ import annotations

from hdl_kgraph.schema import NodeKind


def file_node_id(relpath: str) -> str:
    """Id of the FILE node for *relpath*."""
    return f"file:{relpath}"


def filelist_node_id(relpath: str) -> str:
    """Id of the FILELIST node for *relpath*."""
    return f"filelist:{relpath}"


def decl_node_id(relpath: str, kind: NodeKind, scope_path: str) -> str:
    """Id of a declaration at dotted *scope_path* (e.g. ``top.u_counter``)."""
    return f"{relpath}::{kind.value}:{scope_path}"


def stub_node_id(kind: NodeKind, name: str) -> str:
    """Id of an unresolved-stub node; shared by all referrers of *name*."""
    return f"unresolved:{kind.value}:{name}"
