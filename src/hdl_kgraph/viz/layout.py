"""Precomputed Python-side graph layout for ``visualize`` (viz-scalability
Phase 2).

`d3.forceSimulation` runs on the browser main thread and freezes the tab on
large designs before the first useful frame. Computing node coordinates here
lets the client paint immediately and skip simulation entirely.

A naive global ``networkx.spring_layout`` is itself too slow past ~10k nodes,
so we never run one big layout. Instead we exploit the Louvain communities
already computed for the payload (``metrics.communities``):

1. build the **community quotient graph** (one supernode per community) and
   ``spring_layout`` that small graph to place the communities relative to
   each other;
2. ``spring_layout`` each community's induced subgraph independently (each is
   small) and offset its members by the supernode position, scaled by
   ``sqrt(community size)`` so larger communities claim proportionally more
   room.

Total cost is a sum of small layouts — seconds at 50k nodes — deterministic
under a fixed seed, and visually clustered to match the community coloring
users already see.

numpy + scipy (the ``[layout]`` extra) power networkx's sparse
``spring_layout`` fast path. When they are absent :func:`compute_layout`
returns ``None`` and the caller falls back to the in-browser simulation; the
``visualize`` command never fails for a missing extra.
"""

from __future__ import annotations

import math

import networkx as nx

#: Coordinate space half-extent the community quotient layout is scaled into
#: before members are offset; purely cosmetic (the client re-fits the view).
_COMMUNITY_SPREAD = 1000.0
#: Per-community member spread, multiplied by ``sqrt(size)``.
_MEMBER_SPREAD = 60.0


def layout_available() -> bool:
    """True when the ``[layout]`` extra (numpy **and** scipy) is importable.

    Both matter: networkx's ``spring_layout`` switches to scipy's sparse
    solver for graphs of 500+ nodes — exactly the large-graph case the static
    tier exists for — so checking only numpy would let :func:`compute_layout`
    pick the static tier and then crash inside ``nx.spring_layout`` instead of
    returning ``None`` and falling back to the live simulation.
    """
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        return False
    return True


def compute_layout(
    view: nx.Graph,
    comm_of: dict[str, str],
    *,
    seed: int = 42,
) -> dict[str, tuple[int, int]] | None:
    """Community-stacked layout for *view*.

    *view* is the graph being rendered (the module projection, or the full
    graph in ``--full`` mode). *comm_of* maps node id -> community label;
    nodes absent from it (e.g. ports/signals with no projection community)
    share one synthetic bucket so they still get placed.

    Returns integer ``(x, y)`` per node, or ``None`` when numpy/scipy are not
    installed (the caller then falls back to the live simulation). Coordinates
    are deterministic for a fixed *seed*.
    """
    if not layout_available():
        return None
    if view.number_of_nodes() == 0:
        return {}

    undirected = view.to_undirected()
    # Group nodes by community label; "" and missing ids share one bucket.
    members: dict[str, list[str]] = {}
    for node_id in undirected.nodes():
        label = comm_of.get(node_id) or "_"
        members.setdefault(label, []).append(node_id)

    # 1. Quotient graph: one supernode per community, edge-weighted by the
    #    number of inter-community edges, laid out small-and-fast.
    quotient: nx.Graph = nx.Graph()
    quotient.add_nodes_from(members)
    for u, v in undirected.edges():
        cu = comm_of.get(u) or "_"
        cv = comm_of.get(v) or "_"
        if cu == cv:
            continue
        if quotient.has_edge(cu, cv):
            quotient[cu][cv]["weight"] += 1
        else:
            quotient.add_edge(cu, cv, weight=1)
    # Sorted labels keep iteration order (and thus the layout) stable.
    ordered = sorted(members)
    super_pos = nx.spring_layout(quotient, weight="weight", seed=seed, scale=_COMMUNITY_SPREAD)

    # 2. Lay out each community's induced subgraph, offset by its supernode.
    pos: dict[str, tuple[int, int]] = {}
    for label in ordered:
        ids = members[label]
        cx, cy = super_pos[label]
        spread = _MEMBER_SPREAD * math.sqrt(len(ids))
        if len(ids) == 1:
            pos[ids[0]] = (round(cx), round(cy))
            continue
        sub = undirected.subgraph(ids)
        local = nx.spring_layout(sub, weight="weight", seed=seed, scale=spread)
        for node_id, (lx, ly) in local.items():
            pos[node_id] = (round(cx + lx), round(cy + ly))
    return pos
