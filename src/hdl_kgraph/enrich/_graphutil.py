"""Graph-walk helpers shared by the enrichment backends (M7).

Reconciliation in every backend asks the same two questions of a heuristic
``INSTANCE`` node: *what declares it?* and *what does it instantiate?*. These
live here so neither backend imports the other — :mod:`slang_backend` climbs to
the enclosing ``MODULE`` (SystemVerilog), :mod:`ghdl_backend` to the enclosing
``ARCHITECTURE`` (VHDL), and both follow the same ``INSTANTIATES`` edge out.
"""

from __future__ import annotations

import networkx as nx

from hdl_kgraph.schema import EdgeKind, NodeKind


def enclosing_module(graph: nx.MultiDiGraph, inst_id: str) -> tuple[str, str] | None:
    """The (id, name) of the MODULE that DECLARES *inst_id* (SystemVerilog)."""
    for pred in graph.predecessors(inst_id):
        for data in graph[pred][inst_id].values():
            if (
                data.get("kind") is EdgeKind.DECLARES
                and graph.nodes[pred]["kind"] is NodeKind.MODULE
            ):
                return pred, graph.nodes[pred]["name"]
    return None


def enclosing_architecture(
    graph: nx.MultiDiGraph, inst_id: str
) -> tuple[str, tuple[str, str]] | None:
    """The (id, (of_entity, arch_name)) of the ARCHITECTURE that DECLARES *inst_id*.

    VHDL's container is an architecture (an entity may have several), so the key
    is the lowercased ``(of_entity, architecture)`` pair the backend can match
    against GHDL's resolved binding scope.
    """
    for pred in graph.predecessors(inst_id):
        for data in graph[pred][inst_id].values():
            if (
                data.get("kind") is EdgeKind.DECLARES
                and graph.nodes[pred]["kind"] is NodeKind.ARCHITECTURE
            ):
                node = graph.nodes[pred]
                return pred, (str(node["attrs"].get("of_entity", "")), node["name"])
    return None


def instantiates_target(graph: nx.MultiDiGraph, inst_id: str) -> tuple[str, str] | None:
    """The (id, name) the syntactic INSTANTIATES edge of *inst_id* points at."""
    for succ in graph.successors(inst_id):
        for data in graph[inst_id][succ].values():
            if data.get("kind") is EdgeKind.INSTANTIATES:
                return succ, graph.nodes[succ]["name"]
    return None
