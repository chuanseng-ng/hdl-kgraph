"""Enrichment-delta summary (M7 reporting).

The enrichment pass (:mod:`hdl_kgraph.enrich.runner`) stamps everything it
contributes onto the persisted graph: upgraded edges carry
``attrs["source"] = "elaborated"``, unrolled iterations become ``elab:``-prefixed
nodes (see :func:`hdl_kgraph.ids.elab_node_id`), and a generate/array's syntactic
instance is annotated with ``attrs["elaborated_count"]``. Those stamps make the
"what did ``--enrich`` change vs the default build" delta fully reconstructable
from the saved graph alone — no second heuristic build or rebuild needed.

:func:`summarize_enrichment` reads a loaded :class:`networkx.MultiDiGraph` and
tallies that delta; both the standalone ``enriched`` command and the
``build --enrich`` report use it, so the two surfaces never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from hdl_kgraph.schema import NodeKind

#: Provenance stamp written on every elaboration-derived edge/node.
_ELABORATED = "elaborated"


def _is_elab(node_id: str) -> bool:
    """Whether *node_id* is an elaboration-derived (M7) node id."""
    return node_id.startswith("elab:")


@dataclass
class EnrichmentSummary:
    """What enrichment added/changed relative to the heuristic-only build."""

    enriched: bool = False
    backends: list[str] = field(default_factory=list)
    edges_upgraded: int = 0  # heuristic edges promoted to elaboration confidence
    edges_added: int = 0  # new edges touching an elaborated node
    nodes_added: int = 0  # elaborated (`elab:`) nodes, e.g. unrolled iterations
    generates_unrolled: int = 0  # syntactic instances with elaborated_count > 1


def summarize_enrichment(graph: nx.MultiDiGraph) -> EnrichmentSummary:
    """Reconstruct the enrichment delta from *graph*'s persisted stamps."""
    summary = EnrichmentSummary()
    backends: set[str] = set()

    for node_id, data in graph.nodes(data=True):
        attrs = data.get("attrs") or {}
        if _is_elab(node_id) and attrs.get("source") == _ELABORATED:
            summary.nodes_added += 1
            if attrs.get("backend"):
                backends.add(attrs["backend"])
        elif (
            data.get("kind") is NodeKind.INSTANCE
            and isinstance(attrs.get("elaborated_count"), int)
            and attrs["elaborated_count"] > 1
        ):
            summary.generates_unrolled += 1

    for src, dst, data in graph.edges(data=True):
        attrs = data.get("attrs") or {}
        # An edge touching an `elab:` node was created by enrichment by
        # construction; a heuristic-to-heuristic edge counts only once its
        # provenance stamp marks it as elaboration-confirmed.
        if _is_elab(src) or _is_elab(dst):
            summary.edges_added += 1
        elif attrs.get("source") == _ELABORATED:
            summary.edges_upgraded += 1
        else:
            continue
        if attrs.get("backend"):
            backends.add(attrs["backend"])

    summary.backends = sorted(backends)
    summary.enriched = bool(
        summary.edges_upgraded
        or summary.edges_added
        or summary.nodes_added
        or summary.generates_unrolled
    )
    return summary
