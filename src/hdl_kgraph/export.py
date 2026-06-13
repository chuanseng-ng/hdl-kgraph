"""Graph export to GraphML / GEXF / JSON (viz-scalability Phase 5).

The ``visualize`` HTML artifact is self-contained and air-gapped, but it stops
being viable at extreme scale (~>250k nodes): the file balloons and no browser
renders a million-node hairball usefully. ``export`` is the honest escape
hatch — hand the graph to a dedicated tool. Gephi's OpenOrd/ForceAtlas2 layouts
handle million-node graphs and Cytoscape covers the analysis crowd, both via
the standard GraphML/GEXF interchange formats (or plain node-link JSON).

NetworkX ships the writers; the only work here is sanitizing the graph first.
``builder`` stores attributes the GraphML/GEXF writers cannot serialize — the
``NodeKind``/``EdgeKind``/``Language`` enums, the ``line_span`` tuple, and the
free-form ``attrs`` dict. :func:`_sanitize` flattens these into scalar
attributes (enums to their ``.value``, the span into two ints, ``attrs`` into a
JSON string so nothing is lost) on a throwaway copy, leaving the live graph
untouched.
"""

from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any

import networkx as nx

#: Accepted ``--format`` values; pinned here so the CLI and tests agree.
EXPORT_FORMATS = ("graphml", "gexf", "json")


def _scalar(value: Any) -> Any:
    """Coerce one attribute value to a GraphML/GEXF-safe scalar."""
    if isinstance(value, enum.Enum):
        return value.value
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    # Tuples/lists/dicts and anything else: stringify so the writer accepts it.
    return str(value)


def _flatten_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Return a scalar-only copy of one node/edge attribute mapping."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "line_span":
            # Tuple -> two ints the writers can take; guard short/missing spans.
            span: tuple[Any, ...] = tuple(value) if value else ()
            out["line_start"] = int(span[0]) if len(span) > 0 else 0
            out["line_end"] = int(span[1]) if len(span) > 1 else 0
        elif key == "attrs":
            # Free-form dict: preserve losslessly as a JSON string.
            out["attrs_json"] = json.dumps(value, default=str, sort_keys=True)
        else:
            out[key] = _scalar(value)
    return out


def _sanitize(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """A copy of *g* with only scalar node/edge attributes."""
    clean: nx.MultiDiGraph = nx.MultiDiGraph()
    for node_id, data in g.nodes(data=True):
        clean.add_node(node_id, **_flatten_attrs(data))
    for u, v, data in g.edges(data=True):
        clean.add_edge(u, v, **_flatten_attrs(data))
    return clean


def export_graph(g: nx.MultiDiGraph, out_path: Path, fmt: str) -> Path:
    """Write *g* to *out_path* in *fmt* (one of :data:`EXPORT_FORMATS`).

    Returns the written path. Raises :class:`ValueError` for an unknown *fmt*.
    """
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {EXPORT_FORMATS}, got {fmt!r}")
    out_path = Path(out_path)
    clean = _sanitize(g)
    if fmt == "graphml":
        nx.write_graphml(clean, out_path)
    elif fmt == "gexf":
        nx.write_gexf(clean, out_path)
    else:  # json
        # edges="links" pins the key name and silences the networkx
        # FutureWarning about the changing default.
        data = nx.node_link_data(clean, edges="links")
        out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out_path
