"""Enrichment driver (M7): run backends and merge their deltas into the graph.

Sits between pass-2 linking and persistence in :mod:`hdl_kgraph.pipeline`. The
graph is mutated in place — backends only add nodes/edges, upgrade existing
edges' confidence, and annotate nodes; nothing is deleted, so the tree-sitter
baseline always survives.
"""

from __future__ import annotations

import networkx as nx

from hdl_kgraph.enrich.base import (
    EnrichmentBackend,
    EnrichmentInput,
    EnrichmentReport,
)
from hdl_kgraph.graph.builder import add_or_upgrade_edge, ensure_node
from hdl_kgraph.schema import Edge


def available_backends(names: list[str] | None = None) -> list[EnrichmentBackend]:
    """The installed enrichment backends, optionally filtered to *names*.

    The registry is built lazily so importing this module never imports the
    optional native frontends. M7 phase 1 ships only the pyslang backend; the
    GHDL/VHDL backend slots in here when it lands.
    """
    from hdl_kgraph.enrich.slang_backend import SlangBackend

    registry: list[EnrichmentBackend] = [SlangBackend()]
    selected = [b for b in registry if names is None or b.name in names]
    return [b for b in selected if b.available()]


def run_enrichment(
    graph: nx.MultiDiGraph,
    inp: EnrichmentInput,
    backends: list[EnrichmentBackend],
) -> EnrichmentReport:
    """Run each backend over *inp* and apply its delta to *graph* in place."""
    report = EnrichmentReport()
    for backend in backends:
        files = [p for p in inp.files if p.suffix in backend.suffixes]
        if not files:
            continue
        result = backend.enrich(
            EnrichmentInput(
                files=files,
                defines=inp.defines,
                incdirs=inp.incdirs,
                tops=inp.tops,
                base=inp.base,
            ),
            graph,
        )
        report.backends.append(backend.name)
        for node in result.new_nodes:
            before = graph.number_of_nodes()
            ensure_node(graph, node)
            report.nodes_added += graph.number_of_nodes() - before
        for edge in result.new_edges:
            # New elaborated edges between nodes the backend just added (or the
            # existing module endpoints); upgrade=True so a re-run is a no-op.
            add_or_upgrade_edge(graph, edge, upgrade=True)
        for upgrade in result.upgrades:
            if not (graph.has_node(upgrade.src) and graph.has_node(upgrade.dst)):
                continue
            upgraded = add_or_upgrade_edge(
                graph,
                Edge(
                    src=upgrade.src,
                    dst=upgrade.dst,
                    kind=upgrade.kind,
                    confidence=upgrade.confidence,
                    attrs=upgrade.attrs,
                ),
                upgrade=True,
            )
            if upgraded:
                report.edges_upgraded += 1
        for node_id, extra in result.node_annotations.items():
            if graph.has_node(node_id):
                graph.nodes[node_id]["attrs"].update(extra)
        report.discrepancies.extend(result.discrepancies)
        report.diagnostics.extend(result.diagnostics)
    return report
