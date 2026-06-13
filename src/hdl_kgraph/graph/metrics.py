"""Graph metrics over the module-level projection (M5).

The projection collapses the knowledge graph to design units and weighted
instantiation edges: ``A -> B`` with weight = how many instances of B are
declared in A (a VHDL entity absorbs its architectures' instances, mirroring
``hierarchy_tree``). Unresolved stub targets stay in the projection so a
heavily-referenced missing module still shows up as a hub.

* fan-in / fan-out — weighted degree: how many instantiation sites point at
  a unit / how many instances it contains.
* hubs and bridges — betweenness centrality on the projection (sampled with
  a fixed seed above ``BETWEENNESS_EXACT_MAX_NODES`` units); true cut
  vertices additionally flagged via articulation points on the undirected
  view.
* communities — Louvain (``networkx.community.louvain_communities``) on the
  undirected weighted projection with a fixed seed, so repeated runs of
  ``metrics --communities`` agree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from hdl_kgraph.schema import EdgeKind, NodeKind

#: Design-unit kinds that appear in the projection.
_UNIT_KINDS = frozenset({NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.ENTITY})

#: Projections larger than this get sampled betweenness instead of exact
#: (exact is O(V*E): fine at 1k units, minutes at 10k).
BETWEENNESS_EXACT_MAX_NODES = 500
#: Pivot-sample size (``k``) for approximate betweenness.
BETWEENNESS_SAMPLES = 256
#: Fixed seed so repeated sampled runs agree (same convention as communities()).
BETWEENNESS_SEED = 42


@dataclass
class ModuleMetrics:
    """Structural metrics for one design unit."""

    node_id: str
    name: str
    kind: NodeKind
    file: str
    fan_in: int  # instantiation sites pointing here (weighted)
    fan_out: int  # instances declared here (weighted)
    betweenness: float
    is_articulation: bool  # removing it disconnects the projection
    unresolved: bool


@dataclass
class MetricsResult:
    """``module_metrics`` output: ranked per-unit metrics plus run-level flags."""

    modules: list[ModuleMetrics]
    betweenness_approximate: bool = False  # sampled with k=BETWEENNESS_SAMPLES


def _unit_of(g: nx.MultiDiGraph, node_id: str) -> str | None:
    """The projection unit declaring *node_id* (architectures -> entity)."""
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current not in seen:
        seen.add(current)
        data = g.nodes[current]
        if data["kind"] in _UNIT_KINDS:
            return current
        if data["kind"] is NodeKind.ARCHITECTURE:
            for _, entity, d in g.out_edges(current, data=True):
                if d["kind"] is EdgeKind.IMPLEMENTS:
                    return entity
            return None
        parents = [
            u for u, _, d in g.in_edges(current, data=True) if d["kind"] is EdgeKind.DECLARES
        ]
        current = parents[0] if parents else None
    return None


def module_projection(g: nx.MultiDiGraph) -> nx.DiGraph:
    """Design units + weighted instantiation edges."""
    proj = nx.DiGraph()
    for node_id, data in g.nodes(data=True):
        if data["kind"] in _UNIT_KINDS:
            proj.add_node(
                node_id,
                name=data["name"],
                kind=data["kind"],
                file=data["file"],
                unresolved=bool(data["attrs"].get("unresolved")),
            )
    for inst_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.INSTANCE:
            continue
        parent = _unit_of(g, inst_id)
        if parent is None or parent not in proj:
            continue
        for _, target, d in g.out_edges(inst_id, data=True):
            if d["kind"] is not EdgeKind.INSTANTIATES or target not in proj:
                continue
            if proj.has_edge(parent, target):
                proj[parent][target]["weight"] += 1
            else:
                proj.add_edge(parent, target, weight=1)
    return proj


def module_metrics(g: nx.MultiDiGraph) -> MetricsResult:
    """Per-unit metrics, hubs first (descending betweenness).

    Above ``BETWEENNESS_EXACT_MAX_NODES`` projection nodes, betweenness is
    estimated from ``BETWEENNESS_SAMPLES`` pivots with a fixed seed (so
    repeated runs agree) and ``betweenness_approximate`` is set on the result.
    """
    proj = module_projection(g)
    n = proj.number_of_nodes()
    approximate = n > BETWEENNESS_EXACT_MAX_NODES
    if approximate:
        betweenness = nx.betweenness_centrality(
            proj, k=min(n, BETWEENNESS_SAMPLES), seed=BETWEENNESS_SEED
        )
    else:
        betweenness = nx.betweenness_centrality(proj)
    articulation = (
        set(nx.articulation_points(proj.to_undirected())) if proj.number_of_nodes() else set()
    )
    records = [
        ModuleMetrics(
            node_id=node_id,
            name=data["name"],
            kind=data["kind"],
            file=data["file"],
            fan_in=int(proj.in_degree(node_id, weight="weight")),
            fan_out=int(proj.out_degree(node_id, weight="weight")),
            betweenness=betweenness.get(node_id, 0.0),
            is_articulation=node_id in articulation,
            unresolved=data["unresolved"],
        )
        for node_id, data in proj.nodes(data=True)
    ]
    records.sort(key=lambda r: (-r.betweenness, -(r.fan_in + r.fan_out), r.name))
    return MetricsResult(modules=records, betweenness_approximate=approximate)


def communities(g: nx.MultiDiGraph, seed: int = 42) -> list[list[str]]:
    """Louvain communities of the projection (node ids), largest first.

    The fixed *seed* keeps the partition deterministic across runs; Louvain
    is still order-sensitive across NetworkX versions, so treat communities
    as subsystem *suggestions*, not ground truth.
    """
    proj = module_projection(g)
    if proj.number_of_nodes() == 0:
        return []
    parts = nx.community.louvain_communities(proj.to_undirected(), weight="weight", seed=seed)
    return sorted((sorted(part) for part in parts), key=lambda p: (-len(p), p))
