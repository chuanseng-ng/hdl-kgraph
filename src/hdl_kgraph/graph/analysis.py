"""Analyses over the knowledge graph.

M1 ships the structural queries behind the ``tree`` and ``query`` CLI
commands. Later milestones add impact radius (M4), clock-domain / CDC and
lint-flavored reports, graph metrics, and UVM topology (M5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from hdl_kgraph.schema import EdgeKind, NodeKind


def _is_stub(g: nx.MultiDiGraph, node_id: str) -> bool:
    return bool(g.nodes[node_id]["attrs"].get("unresolved"))


def _edges_of_kind(g: nx.MultiDiGraph, node_id: str, kind: EdgeKind, reverse: bool = False):
    edges = g.in_edges(node_id, data=True) if reverse else g.out_edges(node_id, data=True)
    return [(u, v, d) for u, v, d in edges if d["kind"] is kind]


def find_top_modules(g: nx.MultiDiGraph) -> list[str]:
    """MODULE nodes that are never instantiated (excluding unresolved stubs)."""
    tops = [
        node_id
        for node_id, data in g.nodes(data=True)
        if data["kind"] is NodeKind.MODULE
        and not _is_stub(g, node_id)
        and not _edges_of_kind(g, node_id, EdgeKind.INSTANTIATES, reverse=True)
    ]
    return sorted(tops, key=lambda n: g.nodes[n]["qualified_name"])


@dataclass
class HierarchyNode:
    """One level of the design hierarchy under a module."""

    module_id: str
    module_name: str
    instance_name: str | None  # None for the root
    confidence: float = 1.0
    unresolved: bool = False
    children: list["HierarchyNode"] = field(default_factory=list)
    truncated: bool = False  # depth limit or instantiation cycle reached


def hierarchy_tree(g: nx.MultiDiGraph, top_id: str, max_depth: int = 64) -> HierarchyNode:
    """Design hierarchy from *top_id* via DECLARES(module->instance) +
    INSTANTIATES(instance->module), with a cycle/repeat guard."""

    def expand(module_id: str, instance_name: str | None, conf: float, seen: frozenset[str],
               depth: int) -> HierarchyNode:
        data = g.nodes[module_id]
        node = HierarchyNode(
            module_id=module_id,
            module_name=data["name"],
            instance_name=instance_name,
            confidence=conf,
            unresolved=_is_stub(g, module_id),
        )
        if depth >= max_depth or module_id in seen:
            node.truncated = module_id in seen
            return node
        for _, inst_id, decl in _edges_of_kind(g, module_id, EdgeKind.DECLARES):
            if g.nodes[inst_id]["kind"] is not NodeKind.INSTANCE:
                continue
            for _, child_id, inst_edge in _edges_of_kind(g, inst_id, EdgeKind.INSTANTIATES):
                node.children.append(
                    expand(
                        child_id,
                        g.nodes[inst_id]["name"],
                        inst_edge["confidence"],
                        seen | {module_id},
                        depth + 1,
                    )
                )
        node.children.sort(key=lambda c: (c.instance_name or "", c.module_name))
        return node

    return expand(top_id, None, 1.0, frozenset(), 0)


def instances_of(g: nx.MultiDiGraph, name: str) -> list[dict[str, Any]]:
    """All instantiation sites of design units named *name*.

    Returns one record per INSTANTIATES edge pointing at a matching
    definition (or stub): instance id/name, parent scope, file, line,
    confidence.
    """
    results: list[dict[str, Any]] = []
    for target_id, data in g.nodes(data=True):
        if data["name"] != name or data["kind"] not in (
            NodeKind.MODULE,
            NodeKind.INTERFACE,
            NodeKind.PROGRAM,
            NodeKind.PRIMITIVE,
        ):
            continue
        for inst_id, _, edge in _edges_of_kind(g, target_id, EdgeKind.INSTANTIATES, reverse=True):
            inst = g.nodes[inst_id]
            results.append(
                {
                    "instance_id": inst_id,
                    "instance_name": inst["name"],
                    "qualified_name": inst["qualified_name"],
                    "file": inst["file"],
                    "line": inst["line_span"][0],
                    "confidence": edge["confidence"],
                    "target_unresolved": _is_stub(g, target_id),
                }
            )
    return sorted(results, key=lambda r: (r["file"], r["line"]))


def unresolved_stubs(g: nx.MultiDiGraph) -> list[dict[str, Any]]:
    """All unresolved stub nodes and the ids of nodes referencing them."""
    results: list[dict[str, Any]] = []
    for node_id, data in g.nodes(data=True):
        if not data["attrs"].get("unresolved"):
            continue
        referrers = sorted({u for u, _, d in g.in_edges(node_id, data=True)
                            if d["kind"] is not EdgeKind.DECLARES})
        results.append(
            {
                "id": node_id,
                "kind": data["kind"],
                "name": data["qualified_name"],
                "referrers": referrers,
            }
        )
    return sorted(results, key=lambda r: r["id"])
