"""Self-contained HTML visualization (M5 ``visualize``).

One output file, no network: the vendored ``d3.v7.min.js`` (ISC; see
``static/LICENSE.d3``) and the graph JSON are spliced into
``template.html``, so the artifact opens air-gapped and can be attached to
a review or bug report as-is.

Two payload shapes keep large designs usable:

* default — the module-level instantiation projection (one node per design
  unit) plus the hierarchy tree(s); this is what keeps a 1k-module design
  from freezing the force layout.
* ``full=True`` — every node and edge, for small designs or deep dives;
  the template's kind filters default the noisy kinds off.

Signals and processes carry their clock-domain name (alias-merged
representative clock) so the tooltip can report it. Design units carry
their Louvain community index (module projection) so the force view can
color/filter by subsystem.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph import analysis, clocks, metrics
from hdl_kgraph.graph.analysis import HierarchyNode
from hdl_kgraph.schema import Language, NodeKind

_DATA_MARKER = "/*__DATA__*/"
_D3_MARKER = "/*__D3__*/"


def _tree_to_dict(node: HierarchyNode) -> dict[str, Any]:
    return {
        "id": node.module_id,
        "module": node.module_name,
        "instance": node.instance_name,
        "confidence": node.confidence,
        "unresolved": node.unresolved,
        "architecture": node.architecture,
        "truncated": node.truncated,
        "children": [_tree_to_dict(c) for c in node.children],
    }


def _domain_map(g: nx.MultiDiGraph) -> dict[str, str]:
    """node id -> representative clock name, for domain coloring."""
    domains: dict[str, str] = {}
    for domain in clocks.clock_domains(g):
        label = domain.clock_names[0]
        for node_id in (*domain.process_ids, *domain.signal_ids, domain.clock_id):
            domains[node_id] = label
    return domains


def _payload(g: nx.MultiDiGraph, full: bool, top: str | None, title: str) -> dict[str, Any]:
    domains = _domain_map(g)
    # Louvain communities of the module projection: design-unit nodes carry
    # their community index so the force view can color/filter by subsystem.
    comm_of: dict[str, str] = {}
    for i, part in enumerate(metrics.communities(g)):
        for node_id in part:
            comm_of[node_id] = str(i)
    if full:
        nodes = [
            {
                "id": node_id,
                "name": data["qualified_name"] or data["name"],
                "kind": data["kind"].value,
                "file": data["file"],
                "line": data["line_span"][0],
                "domain": domains.get(node_id, ""),
                "community": comm_of.get(node_id, ""),
                "unresolved": bool(data["attrs"].get("unresolved")),
            }
            for node_id, data in g.nodes(data=True)
        ]
        links = [
            {
                "source": u,
                "target": v,
                "kind": d["kind"].value,
                "confidence": d["confidence"],
            }
            for u, v, d in g.edges(data=True)
        ]
    else:
        proj = metrics.module_projection(g)
        nodes = [
            {
                "id": node_id,
                "name": data["name"],
                "kind": data["kind"].value,
                "file": data["file"],
                "line": 0,
                "domain": "",
                "community": comm_of.get(node_id, ""),
                "unresolved": data["unresolved"],
            }
            for node_id, data in proj.nodes(data=True)
        ]
        links = [
            {
                "source": u,
                "target": v,
                "kind": "instantiates",
                "confidence": 1.0,
                "weight": d["weight"],
            }
            for u, v, d in proj.edges(data=True)
        ]

    if top is not None:
        roots = [
            node_id
            for node_id, data in g.nodes(data=True)
            if data["kind"] in (NodeKind.MODULE, NodeKind.ENTITY)
            # VHDL names are stored lowercase (case-insensitive); SV is exact.
            and data["name"] == (top.lower() if data["language"] is Language.VHDL else top)
            and not data["attrs"].get("unresolved")
        ]
        if not roots:  # a typo would otherwise render an empty page
            raise ValueError(f"module or entity {top!r} not found in the graph")
    else:
        roots = analysis.find_top_modules(g)
    hierarchy = [_tree_to_dict(analysis.hierarchy_tree(g, root)) for root in roots]

    return {
        "title": title,
        "full": full,
        "nodes": nodes,
        "links": links,
        "hierarchy": hierarchy,
        "communities": sorted({c for c in comm_of.values()}, key=int),
    }


def render_html(
    g: nx.MultiDiGraph,
    out_path: Path,
    *,
    full: bool = False,
    top: str | None = None,
    title: str = "hdl-kgraph",
) -> Path:
    """Render the graph to a single self-contained HTML file.

    Raises :class:`ValueError` when *top* names no module or entity.
    """
    package = resources.files("hdl_kgraph.viz")
    template = (package / "template.html").read_text(encoding="utf-8")
    d3 = (package / "static" / "d3.v7.min.js").read_text(encoding="utf-8")
    # "</" must not appear verbatim inside an inline <script> payload.
    data = json.dumps(_payload(g, full, top, title)).replace("</", "<\\/")
    html = template.replace(_D3_MARKER, d3, 1).replace(_DATA_MARKER, data, 1)
    out_path = Path(out_path)
    out_path.write_text(html, encoding="utf-8")
    return out_path
