"""Analyses over the knowledge graph.

M1 ships the structural queries behind the ``tree`` and ``query`` CLI
commands. Later milestones add impact radius (M4), clock-domain / CDC and
lint-flavored reports, graph metrics, and UVM topology (M5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from hdl_kgraph.schema import EdgeKind, Language, NodeKind


def _is_stub(g: nx.MultiDiGraph, node_id: str) -> bool:
    return bool(g.nodes[node_id]["attrs"].get("unresolved"))


def _edges_of_kind(
    g: nx.MultiDiGraph, node_id: str, kind: EdgeKind, reverse: bool = False
) -> list[tuple[str, str, dict[str, Any]]]:
    edges = g.in_edges(node_id, data=True) if reverse else g.out_edges(node_id, data=True)
    return [(u, v, d) for u, v, d in edges if d["kind"] is kind]


#: Design-unit kinds that root a hierarchy (SV modules and VHDL entities).
_HIERARCHY_ROOT_KINDS = (NodeKind.MODULE, NodeKind.ENTITY)


def find_top_modules(g: nx.MultiDiGraph) -> list[str]:
    """MODULE/ENTITY nodes never instantiated (excluding unresolved stubs)."""
    tops = [
        node_id
        for node_id, data in g.nodes(data=True)
        if data["kind"] in _HIERARCHY_ROOT_KINDS
        and not _is_stub(g, node_id)
        and not _edges_of_kind(g, node_id, EdgeKind.INSTANTIATES, reverse=True)
    ]
    return sorted(tops, key=lambda n: g.nodes[n]["qualified_name"])


@dataclass
class HierarchyNode:
    """One level of the design hierarchy under a module/entity."""

    module_id: str
    module_name: str
    instance_name: str | None  # None for the root
    confidence: float = 1.0
    unresolved: bool = False
    architecture: str | None = None  # the VHDL architecture expanded, if one
    children: list[HierarchyNode] = field(default_factory=list)
    truncated: bool = False  # depth limit or instantiation cycle reached


def _instance_holders(g: nx.MultiDiGraph, unit_id: str, via_arch: str | None) -> list[str]:
    """Scopes whose declared instances are *unit_id*'s children.

    A MODULE holds its instances directly; an ENTITY's instances live in its
    ARCHITECTURE(s), reached via reverse IMPLEMENTS — narrowed to *via_arch*
    when the instantiation site named one (``entity work.alu(rtl)``).
    """
    holders = [unit_id]
    archs = [u for u, _, d in g.in_edges(unit_id, data=True) if d["kind"] is EdgeKind.IMPLEMENTS]
    if via_arch:
        named = [a for a in archs if g.nodes[a]["name"] == via_arch]
        archs = named or archs
    holders.extend(sorted(archs, key=lambda a: g.nodes[a]["qualified_name"]))
    return holders


def hierarchy_tree(g: nx.MultiDiGraph, top_id: str, max_depth: int = 64) -> HierarchyNode:
    """Design hierarchy from *top_id* via DECLARES(module->instance) +
    INSTANTIATES(instance->module), with a cycle/repeat guard. VHDL entities
    expand through their architectures (reverse IMPLEMENTS)."""

    def has_instances(unit_id: str, via_arch: str | None) -> bool:
        return any(
            g.nodes[inst_id]["kind"] is NodeKind.INSTANCE
            for holder in _instance_holders(g, unit_id, via_arch)
            for _, inst_id, _ in _edges_of_kind(g, holder, EdgeKind.DECLARES)
        )

    def expand(
        module_id: str,
        instance_name: str | None,
        conf: float,
        seen: frozenset[str],
        depth: int,
        via_arch: str | None = None,
    ) -> HierarchyNode:
        data = g.nodes[module_id]
        node = HierarchyNode(
            module_id=module_id,
            module_name=data["name"],
            instance_name=instance_name,
            confidence=conf,
            unresolved=_is_stub(g, module_id),
        )
        holders = _instance_holders(g, module_id, via_arch)
        archs = holders[1:]
        if len(archs) == 1:
            node.architecture = g.nodes[archs[0]]["name"]
        if depth >= max_depth or module_id in seen:
            # A cycle is always a truncation; a depth-capped node only is one
            # if it actually had children left to expand.
            node.truncated = module_id in seen or has_instances(module_id, via_arch)
            return node
        for holder in holders:
            for _, inst_id, _decl in _edges_of_kind(g, holder, EdgeKind.DECLARES):
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
                            via_arch=inst_edge["attrs"].get("architecture"),
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
        if data["kind"] not in (
            NodeKind.MODULE,
            NodeKind.INTERFACE,
            NodeKind.PROGRAM,
            NodeKind.PRIMITIVE,
            NodeKind.ENTITY,
        ):
            continue
        # VHDL names are stored lowercase and match case-insensitively.
        wanted = name.lower() if data["language"] is Language.VHDL else name
        if data["name"] != wanted:
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
        referrers = sorted(
            {u for u, _, d in g.in_edges(node_id, data=True) if d["kind"] is not EdgeKind.DECLARES}
        )
        results.append(
            {
                "id": node_id,
                "kind": data["kind"],
                "name": data["qualified_name"],
                "referrers": referrers,
            }
        )
    return sorted(results, key=lambda r: r["id"])
