"""Analyses over the knowledge graph.

M1 ships the structural queries behind the ``tree`` and ``query`` CLI
commands; M4 adds the impact radius behind ``impact``. Later milestones add
clock-domain / CDC and lint-flavored reports, graph metrics, and UVM
topology (M5).
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


#: Kinds reported as "affected design units" by the impact radius.
IMPACT_UNIT_KINDS = frozenset(
    {
        NodeKind.MODULE,
        NodeKind.INTERFACE,
        NodeKind.PROGRAM,
        NodeKind.PRIMITIVE,
        NodeKind.PACKAGE,
        NodeKind.CHECKER,
        NodeKind.CLASS,
        NodeKind.ENTITY,
        NodeKind.ARCHITECTURE,
        NodeKind.VHDL_PACKAGE,
        NodeKind.PACKAGE_BODY,
        NodeKind.CONFIGURATION,
    }
)


@dataclass
class ImpactRecord:
    """One node transitively affected by a change (``impact`` command)."""

    node_id: str
    kind: NodeKind
    name: str
    file: str
    line: int
    depth: int  # BFS distance from the seed(s)
    via: EdgeKind  # the edge kind that pulled this node in


def _enclosing_unit(g: nx.MultiDiGraph, node_id: str) -> str | None:
    """Climb reverse DECLARES from *node_id* to the unit that contains it."""
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current not in seen:
        seen.add(current)
        if g.nodes[current]["kind"] in IMPACT_UNIT_KINDS:
            return current
        parents = [
            u for u, _, d in g.in_edges(current, data=True) if d["kind"] is EdgeKind.DECLARES
        ]
        current = parents[0] if parents else None
    return None


def _impact_dependents(g: nx.MultiDiGraph, node_id: str) -> list[tuple[str | None, EdgeKind]]:
    """Nodes that depend on *node_id* — one BFS step of the impact radius.

    Design units propagate through reverse ``INSTANTIATES`` (to the
    instantiating unit), ``IMPORTS``/``USES_PACKAGE`` (to the importing
    scope's unit), ``EXTENDS`` (to subclasses), ``BINDS`` (to the binding
    configuration), and ``IMPLEMENTS`` both ways (an entity change affects
    its architectures; an architecture change affects its entity, and from
    there the entity's instantiators). FILE nodes propagate through reverse
    ``INCLUDES``, macro definitions to their users (``DEFINES_MACRO`` →
    reverse ``USES_MACRO``), and to the units they declare.
    """
    dependents: list[tuple[str | None, EdgeKind]] = []
    kind = g.nodes[node_id]["kind"]
    if kind is NodeKind.FILE:
        for src, _, data in g.in_edges(node_id, data=True):
            if data["kind"] is EdgeKind.INCLUDES:
                dependents.append((src, EdgeKind.INCLUDES))
        for _, dst, data in g.out_edges(node_id, data=True):
            if data["kind"] is EdgeKind.DEFINES_MACRO:
                for user, _, use in g.in_edges(dst, data=True):
                    if use["kind"] is EdgeKind.USES_MACRO:
                        dependents.append((user, EdgeKind.USES_MACRO))
            elif data["kind"] is EdgeKind.DECLARES and g.nodes[dst]["kind"] in IMPACT_UNIT_KINDS:
                dependents.append((dst, EdgeKind.DECLARES))
        return dependents

    for src, _, data in g.in_edges(node_id, data=True):
        edge_kind = data["kind"]
        if edge_kind is EdgeKind.INSTANTIATES:
            dependents.append((_enclosing_unit(g, src), edge_kind))  # src is the INSTANCE
        elif edge_kind in (EdgeKind.IMPORTS, EdgeKind.USES_PACKAGE, EdgeKind.BINDS):
            dependents.append((_enclosing_unit(g, src), edge_kind))
        elif edge_kind in (EdgeKind.EXTENDS, EdgeKind.IMPLEMENTS):
            dependents.append((src, edge_kind))
    if kind is NodeKind.ARCHITECTURE:
        for _, dst, data in g.out_edges(node_id, data=True):
            if data["kind"] is EdgeKind.IMPLEMENTS:
                dependents.append((dst, EdgeKind.IMPLEMENTS))
    return dependents


def impact_radius(
    g: nx.MultiDiGraph, seed_ids: list[str], max_depth: int = 0
) -> list[ImpactRecord]:
    """Everything transitively affected by a change to the seed nodes.

    BFS over the reverse-dependency relation of :func:`_impact_dependents`;
    *max_depth* <= 0 means unlimited. Seeds themselves are not reported.
    """
    visited = set(seed_ids)
    frontier = list(seed_ids)
    records: list[ImpactRecord] = []
    depth = 0
    while frontier and (max_depth <= 0 or depth < max_depth):
        depth += 1
        next_frontier: list[str] = []
        for node_id in frontier:
            for dep, via in _impact_dependents(g, node_id):
                if dep is None or dep in visited:
                    continue
                visited.add(dep)
                data = g.nodes[dep]
                records.append(
                    ImpactRecord(
                        node_id=dep,
                        kind=data["kind"],
                        name=data["name"],
                        file=data["file"],
                        line=data["line_span"][0],
                        depth=depth,
                        via=via,
                    )
                )
                next_frontier.append(dep)
        frontier = next_frontier
    return sorted(records, key=lambda r: (r.depth, r.kind.value, r.name, r.node_id))


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
