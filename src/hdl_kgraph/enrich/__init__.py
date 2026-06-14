"""Semantic enrichment via native HDL frontends (M7).

See :mod:`hdl_kgraph.enrich.base` for the backend interface and the overlay
model. :func:`run_enrichment` applies backend deltas to the linked graph; the
pipeline calls it as an opt-in pass-3 stage when ``build --enrich`` is given.
"""

from __future__ import annotations

from hdl_kgraph.enrich.base import (
    Capabilities,
    Discrepancy,
    EdgeUpgrade,
    EnrichmentBackend,
    EnrichmentInput,
    EnrichmentReport,
    EnrichmentResult,
)
from hdl_kgraph.enrich.report import EnrichmentSummary, summarize_enrichment
from hdl_kgraph.enrich.runner import available_backends, run_enrichment

__all__ = [
    "Capabilities",
    "Discrepancy",
    "EdgeUpgrade",
    "EnrichmentBackend",
    "EnrichmentInput",
    "EnrichmentReport",
    "EnrichmentResult",
    "EnrichmentSummary",
    "available_backends",
    "run_enrichment",
    "summarize_enrichment",
]
