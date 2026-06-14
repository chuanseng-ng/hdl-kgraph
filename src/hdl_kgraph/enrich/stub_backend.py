"""A no-op enrichment backend (M7).

Exercises the pipeline integration and the merge plumbing without depending on
a native frontend — it elaborates nothing and returns an empty delta, so a
build that selects only this backend is identical to a non-enriched build. Used
by tests to prove the "works with zero elaboration" path end to end; not part
of the default backend registry.
"""

from __future__ import annotations

import networkx as nx

from hdl_kgraph.enrich.base import Capabilities, EnrichmentInput, EnrichmentResult


class StubBackend:
    """Declares the interface, derives nothing."""

    name = "stub"
    suffixes = frozenset({".v", ".vh", ".sv", ".svh"})

    def available(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def enrich(self, inp: EnrichmentInput, graph: nx.MultiDiGraph) -> EnrichmentResult:
        return EnrichmentResult()
