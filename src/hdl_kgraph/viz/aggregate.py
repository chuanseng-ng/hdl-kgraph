"""Community aggregation for the collapsed ``visualize`` view (viz-scalability
Phase 3).

The collapsed view shows one **supernode per Louvain community** (subsystem
suggestion) instead of every design unit, so a large projection reads as a
handful of subsystems rather than a hairball; the client expands a community in
place on double-click. This module builds that aggregation from data already
computed for the payload — the module projection
(:func:`hdl_kgraph.graph.metrics.module_projection`), the community partition
(:func:`hdl_kgraph.graph.metrics.communities`, stringified as ``comm_of``), and
the hub ranking (:func:`hdl_kgraph.graph.metrics.module_metrics`). Pure Python,
no new dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from hdl_kgraph.graph.metrics import ModuleMetrics


@dataclass
class Aggregation:
    """Collapsed view of the projection: communities and the edges between them."""

    #: One per community: ``{"id": label, "label": repr_name, "count": members}``.
    supernodes: list[dict]
    #: Summed cross-community edge weights: ``{"source", "target", "weight"}``.
    superlinks: list[dict]


@dataclass
class FullAggregation:
    """Two-level collapse of the full graph (``--collapse --full``): communities
    of units, each unit holding its leaf nodes (signals/ports/processes/…)."""

    #: Community level: ``{"id": label, "label": repr_name, "count": units}``.
    supernodes: list[dict]
    #: Unit level: ``{"id": unit_id, "label": name, "community": c, "count": leaves}``.
    unitnodes: list[dict]


def _key(label: str) -> tuple[int, object]:
    """Order community labels numerically (they are stringified ints) but stay
    safe for any other label."""
    return (0, int(label)) if label.isdigit() else (1, label)


def aggregate(
    proj: nx.DiGraph,
    comm_of: dict[str, str],
    ranked: list[ModuleMetrics],
) -> Aggregation:
    """Build the collapsed community view of *proj*.

    *comm_of* maps each projection node id to its community label; *ranked* is
    the hubs-first metrics ranking used to name each community after its most
    central member. Nodes without a community are skipped (they have no
    supernode to belong to).
    """
    # Representative label per community: the first member seen in hub order, so
    # the most central unit names the subsystem.
    label_of: dict[str, str] = {}
    for m in ranked:
        c = comm_of.get(m.node_id)
        if c is not None and c not in label_of:
            label_of[c] = m.name

    counts: dict[str, int] = {}
    for node_id in proj.nodes():
        c = comm_of.get(node_id)
        if c is not None:
            counts[c] = counts.get(c, 0) + 1

    supernodes = [
        {"id": c, "label": label_of.get(c, c), "count": counts[c]} for c in sorted(counts, key=_key)
    ]

    # Sum projection edge weights over each ordered cross-community pair;
    # intra-community edges stay hidden until that community is expanded.
    weights: dict[tuple[str, str], int] = {}
    for u, v, data in proj.edges(data=True):
        cu, cv = comm_of.get(u), comm_of.get(v)
        if cu is None or cv is None or cu == cv:
            continue
        weights[(cu, cv)] = weights.get((cu, cv), 0) + int(data.get("weight", 1))

    superlinks = [
        {"source": s, "target": t, "weight": w}
        for (s, t), w in sorted(weights.items(), key=lambda kv: (_key(kv[0][0]), _key(kv[0][1])))
    ]
    return Aggregation(supernodes=supernodes, superlinks=superlinks)


def aggregate_full(
    comm_of: dict[str, str],
    ranked: list[ModuleMetrics],
    unit_of: dict[str, str],
) -> FullAggregation:
    """Two-level aggregation for ``--collapse --full``.

    *comm_of* maps each design **unit** to its community; *ranked* is the
    hubs-first metrics ranking (used for community labels and unit names);
    *unit_of* maps each non-unit node to its owning unit
    (:func:`hdl_kgraph.graph.metrics.unit_membership`). Produces community
    supernodes (counting their units) and unit supernodes (counting their
    leaves).
    """
    name_of = {m.node_id: m.name for m in ranked}

    label_of: dict[str, str] = {}
    for m in ranked:
        c = comm_of.get(m.node_id)
        if c is not None and c not in label_of:
            label_of[c] = m.name

    units_in_comm: dict[str, int] = {}
    for community in comm_of.values():
        units_in_comm[community] = units_in_comm.get(community, 0) + 1
    supernodes = [
        {"id": c, "label": label_of.get(c, c), "count": units_in_comm[c]}
        for c in sorted(units_in_comm, key=_key)
    ]

    leaves_in_unit: dict[str, int] = {}
    for owner in unit_of.values():
        leaves_in_unit[owner] = leaves_in_unit.get(owner, 0) + 1
    unitnodes = [
        {
            "id": unit,
            "label": name_of.get(unit, unit),
            "community": comm_of[unit],
            "count": leaves_in_unit.get(unit, 0),
        }
        for unit in sorted(comm_of, key=lambda u: (_key(comm_of[u]), u))
    ]
    return FullAggregation(supernodes=supernodes, unitnodes=unitnodes)
