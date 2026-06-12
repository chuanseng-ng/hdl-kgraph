"""Analyses over the knowledge graph.

M1 ships the structural queries behind the ``tree`` and ``query`` CLI
commands; M4 adds the impact radius behind ``impact``. Later milestones add
clock-domain / CDC and lint-flavored reports, graph metrics, and UVM
topology (M5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.discovery import SUFFIXES
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import FileMeta


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


def impact_seeds(g: nx.MultiDiGraph, files: list[FileMeta], target: str) -> list[str]:
    """Resolve an impact *target* (file path first, else unit name) to node ids."""
    known_paths = {f.path for f in files}
    candidate = target.replace("\\", "/").lstrip("./")
    if candidate in known_paths or "/" in candidate or Path(candidate).suffix in SUFFIXES:
        matches = [p for p in known_paths if p == candidate or p.endswith("/" + candidate)]
        return [f"file:{p}" for p in matches if f"file:{p}" in g]
    return [
        node_id
        for node_id, data in g.nodes(data=True)
        if data["kind"] in IMPACT_UNIT_KINDS
        and data["name"] == (target.lower() if data["language"] is Language.VHDL else target)
        and not data["attrs"].get("unresolved")
    ]


def signal_drivers(
    g: nx.MultiDiGraph,
    signal: str,
    module: str | None = None,
    readers: bool = False,
) -> list[dict[str, Any]]:
    """What drives (or, with *readers*, reads) signals/ports named *signal*.

    One record per DRIVES/READS edge into a matching SIGNAL/PORT node.
    *module* narrows matches to signals whose enclosing design unit has that
    name (VHDL names match case-insensitively, as everywhere else).
    """
    kind = EdgeKind.READS if readers else EdgeKind.DRIVES
    records: list[dict[str, Any]] = []
    for node_id, data in g.nodes(data=True):
        if data["kind"] not in (NodeKind.SIGNAL, NodeKind.PORT):
            continue
        is_vhdl = data["language"] is Language.VHDL
        if data["name"] != (signal.lower() if is_vhdl else signal):
            continue
        unit_id = _enclosing_unit(g, node_id)
        unit_name = g.nodes[unit_id]["name"] if unit_id else None
        unit_names = {unit_name} if unit_name else set()
        if unit_id and g.nodes[unit_id]["kind"] is NodeKind.ARCHITECTURE:
            # A VHDL architecture's signals belong to its entity for callers.
            unit_names.update(
                g.nodes[dst]["name"]
                for _, dst, d in g.out_edges(unit_id, data=True)
                if d["kind"] is EdgeKind.IMPLEMENTS
            )
        if module is not None and (module.lower() if is_vhdl else module) not in unit_names:
            continue
        for src, _, edge in g.in_edges(node_id, data=True):
            if edge["kind"] is not kind:
                continue
            site = g.nodes[src]
            span = edge["attrs"].get("line_span") or site["line_span"]
            records.append(
                {
                    "signal_id": node_id,
                    "signal": data["qualified_name"],
                    "module": unit_name,
                    "site_id": src,
                    "site": site["qualified_name"],
                    "site_kind": site["kind"].value,
                    "file": site["file"],
                    "line": span[0] if span else 0,
                    "confidence": edge["confidence"],
                }
            )
    return sorted(records, key=lambda r: (r["signal"], r["file"], r["line"], r["site"]))


#: Kinds a ``port_map``/``find_module`` lookup treats as instantiable units.
INSTANTIABLE_KINDS = frozenset(
    {
        NodeKind.MODULE,
        NodeKind.INTERFACE,
        NodeKind.PROGRAM,
        NodeKind.PRIMITIVE,
        NodeKind.ENTITY,
    }
)


def _match_name(data: dict[str, Any], wanted: str) -> bool:
    """Node-name equality with the project's VHDL case-insensitivity rule."""
    return bool(
        data["name"] == (wanted.lower() if data["language"] is Language.VHDL else wanted)
    )


def _declaration_order(record: dict[str, Any]) -> tuple[bool, Any, int, str]:
    return (record["index"] is None, record["index"], record["line"], record["name"])


def port_map(g: nx.MultiDiGraph, unit: str, instance: str | None = None) -> list[dict[str, Any]]:
    """Ports and parameters of design units named *unit*, in declaration order.

    With *instance*, each unit record also lists the CONNECTS bindings of
    matching instances of that unit (by instance name or qualified name).
    """
    results: list[dict[str, Any]] = []
    for unit_id, data in g.nodes(data=True):
        if data["kind"] not in INSTANTIABLE_KINDS or _is_stub(g, unit_id):
            continue
        if not _match_name(data, unit):
            continue
        ports: list[dict[str, Any]] = []
        parameters: list[dict[str, Any]] = []
        for _, child_id, decl in g.out_edges(unit_id, data=True):
            if decl["kind"] is not EdgeKind.DECLARES:
                continue
            child = g.nodes[child_id]
            record = {
                "id": child_id,
                "name": child["name"],
                "index": child["attrs"].get("index"),
                "line": child["line_span"][0],
                "attrs": child["attrs"],
            }
            if child["kind"] is NodeKind.PORT:
                ports.append({**record, "direction": child["attrs"].get("direction")})
            elif child["kind"] is NodeKind.PARAMETER:
                parameters.append(
                    {**record, "is_localparam": child["attrs"].get("is_localparam", False)}
                )
        unit_record: dict[str, Any] = {
            "unit_id": unit_id,
            "name": data["name"],
            "kind": data["kind"],
            "file": data["file"],
            "line": data["line_span"][0],
            "language": data["language"],
            "ports": sorted(ports, key=_declaration_order),
            "parameters": sorted(parameters, key=_declaration_order),
        }
        if instance is not None:
            unit_record["instances"] = _instance_bindings(g, unit_id, instance)
        results.append(unit_record)
    return sorted(results, key=lambda r: (r["kind"].value, r["file"], r["line"]))


def _instance_bindings(g: nx.MultiDiGraph, unit_id: str, instance: str) -> list[dict[str, Any]]:
    """CONNECTS bindings of instances of *unit_id* matching *instance*."""
    out: list[dict[str, Any]] = []
    for inst_id, _, edge in g.in_edges(unit_id, data=True):
        if edge["kind"] is not EdgeKind.INSTANTIATES:
            continue
        inst = g.nodes[inst_id]
        if instance not in (inst["name"], inst["qualified_name"]):
            continue
        bindings = []
        for _, dst, conn in g.out_edges(inst_id, data=True):
            if conn["kind"] is not EdgeKind.CONNECTS:
                continue
            attrs = conn["attrs"]
            dst_data = g.nodes[dst]
            port_name = attrs.get("port_name")
            if port_name is None and dst_data["kind"] is NodeKind.PORT:
                port_name = dst_data["name"]
            span = attrs.get("line_span")
            bindings.append(
                {
                    "port": port_name,
                    "actual": attrs.get("expr_text"),
                    "position": attrs.get("position"),
                    "wildcard": attrs.get("wildcard", False),
                    "confidence": conn["confidence"],
                    "line": span[0] if span else None,
                }
            )
        bindings.sort(key=lambda b: (b["position"] is None, b["position"], b["port"] or ""))
        out.append(
            {
                "instance_id": inst_id,
                "instance_name": inst["name"],
                "qualified_name": inst["qualified_name"],
                "file": inst["file"],
                "line": inst["line_span"][0],
                "bindings": bindings,
            }
        )
    return sorted(out, key=lambda i: (i["file"], i["line"], i["qualified_name"]))


def search_nodes(
    g: nx.MultiDiGraph,
    name: str = "*",
    kinds: Sequence[NodeKind] | None = None,
    file: str | None = None,
) -> list[dict[str, Any]]:
    """Nodes matching a *name* glob, optionally filtered by kind and file glob.

    The pattern matches the node ``name`` (``qualified_name`` too when it
    contains a ``.``); VHDL nodes match case-insensitively. Unresolved stubs
    are included and flagged so callers can filter them.
    """
    kind_set = frozenset(kinds) if kinds is not None else None
    qualified = "." in name
    results: list[dict[str, Any]] = []
    for node_id, data in g.nodes(data=True):
        if kind_set is not None and data["kind"] not in kind_set:
            continue
        if file is not None and not fnmatchcase(data["file"] or "", file):
            continue
        pattern = name.lower() if data["language"] is Language.VHDL else name
        subject = data["name"].lower() if data["language"] is Language.VHDL else data["name"]
        if not fnmatchcase(subject, pattern) and not (
            qualified and fnmatchcase(data["qualified_name"], name)
        ):
            continue
        results.append(
            {
                "id": node_id,
                "kind": data["kind"],
                "name": data["name"],
                "qualified_name": data["qualified_name"],
                "file": data["file"],
                "line": data["line_span"][0],
                "language": data["language"],
                "unresolved": bool(data["attrs"].get("unresolved")),
            }
        )
    return sorted(results, key=lambda r: (r["kind"].value, r["qualified_name"], r["id"]))


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
