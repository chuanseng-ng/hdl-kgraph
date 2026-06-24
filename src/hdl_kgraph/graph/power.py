"""Power-domain analysis (M10 UPF power intent).

Reads ``POWER_DOMAIN`` nodes (from UPF ``create_power_domain``) and the
``CONSTRAINS`` edges that bind each domain to the design instances it contains,
surfacing every domain with its resolved elements and its isolation/retention/
level-shifter strategies — the power-intent analogue of the clock-domain
report. Everything is name-level and evidence-scored; UPF is never evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from hdl_kgraph.schema import EdgeKind, NodeKind


@dataclass
class PowerDomain:
    """One UPF power domain: its elements and power-management strategies."""

    name: str
    file: str
    line: int
    #: Resolved element instances (qualified names), sorted.
    elements: list[str] = field(default_factory=list)
    #: Element-query names the UPF declared but pass 2 could not resolve
    #: (``.`` — the design root — and instances absent from the parsed design).
    unresolved_elements: list[str] = field(default_factory=list)
    #: Isolation/retention/level-shifter strategy dicts, in declaration order.
    strategies: list[dict[str, Any]] = field(default_factory=list)
    supply: str | None = None
    min_confidence: float = 1.0

    @property
    def isolated(self) -> bool:
        """True when an isolation strategy applies to this domain."""
        return any(s.get("kind") == "isolation" for s in self.strategies)


def power_domains(g: nx.MultiDiGraph) -> list[PowerDomain]:
    """Every UPF power domain with its resolved elements and strategies."""
    domains: list[PowerDomain] = []
    for node_id, data in g.nodes(data=True):
        if data["kind"] is not NodeKind.POWER_DOMAIN:
            continue
        attrs = data["attrs"]
        declared = list(attrs.get("elements") or [])
        resolved: list[str] = []
        min_conf = 1.0
        for _src, dst, edge in g.out_edges(node_id, data=True):
            if edge["kind"] is not EdgeKind.CONSTRAINS:
                continue
            resolved.append(g.nodes[dst].get("qualified_name") or g.nodes[dst]["name"])
            min_conf = min(min_conf, edge["confidence"])
        resolved_names = {name.rsplit(".", 1)[-1] for name in resolved}
        unresolved = [e for e in declared if e == "." or e.rsplit(".", 1)[-1] not in resolved_names]
        domains.append(
            PowerDomain(
                name=data["name"],
                file=data["file"],
                line=data["line_span"][0],
                elements=sorted(resolved),
                unresolved_elements=unresolved,
                strategies=list(attrs.get("strategies") or []),
                supply=attrs.get("supply"),
                min_confidence=min_conf if resolved else 1.0,
            )
        )
    return sorted(domains, key=lambda d: d.name)
