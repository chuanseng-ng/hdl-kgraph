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

import base64
import gzip
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph import analysis, clocks, metrics
from hdl_kgraph.graph.analysis import HierarchyNode
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.viz.layout import compute_layout, layout_available

_DATA_MARKER = "/*__DATA__*/"
_D3_MARKER = "/*__D3__*/"
_ENC_MARKER = "/*__ENC__*/"

#: Layout tiers (viz-scalability Phase 2). Below the live thresholds the
#: client runs ``d3.forceSimulation`` as before; above them we ship
#: precomputed coordinates and the client paints the first frame immediately.
#: Pinned as module constants so tests can assert the routing.
LIVE_MAX_NODES = 2000
LIVE_MAX_EDGES = 6000
#: Upper bound of the static tier; past it the aggregate / export tiers
#: (viz-scalability Phases 3 & 5) are the real answer — until those land we
#: still ship a best-effort static layout and note the over-budget size.
STATIC_MAX_NODES = 50_000

#: Hard cap on the raw (uncompressed) inlined payload, in bytes. Above this the
#: artifact is too large for a browser to open usefully; ``render_html`` refuses
#: with an actionable message unless ``--force-inline`` overrides. Referenced by
#: the module-global name (not a default arg) so tests can monkeypatch it.
#: (viz-scalability Phase 4a.)
MAX_INLINE_BYTES = 75 * 1024 * 1024

#: Raw-JSON size above which the inlined payload is gzip+base64 compressed
#: (viz-scalability Phase 4b) so large-but-compressible designs still embed
#: rather than being refused. Below it the payload stays plain JSON so the
#: artifact is human-inspectable and opens in any browser. Module-global so
#: tests can monkeypatch it, like :data:`MAX_INLINE_BYTES`.
COMPRESS_OVER_BYTES = 2 * 1024 * 1024

#: Accepted ``--layout`` values.
LAYOUT_MODES = ("auto", "live", "static")


@dataclass
class RenderResult:
    """Outcome of :func:`render_html`."""

    path: Path
    layout: str  # the resolved tier actually embedded: "live" or "static"
    node_count: int
    edge_count: int
    note: str  # one-line routing explanation for the CLI ("" when unremarkable)
    compressed: bool = False  # whether the inlined payload is gzip+base64 (Phase 4b)


def _resolve_layout(requested: str, n_nodes: int, n_edges: int, layout_ok: bool) -> RenderResult:
    """Pick the layout tier and a human-readable note (path filled in later)."""

    def result(mode: str, note: str = "") -> RenderResult:
        return RenderResult(Path(), mode, n_nodes, n_edges, note)

    if requested == "live":
        return result("live")
    if requested == "static":
        if not layout_ok:
            return result(
                "live",
                "layout: live — static requested but the [layout] extra "
                "(numpy/scipy) is not installed",
            )
        note = f"layout: static ({n_nodes} nodes, {n_edges} edges)"
        if n_nodes > STATIC_MAX_NODES:
            note += f" — above the {STATIC_MAX_NODES}-node static budget, layout may be slow"
        return result("static", note)
    # auto
    if n_nodes <= LIVE_MAX_NODES and n_edges <= LIVE_MAX_EDGES:
        return result("live")
    trigger = (
        f"{n_nodes} nodes > {LIVE_MAX_NODES}"
        if n_nodes > LIVE_MAX_NODES
        else f"{n_edges} edges > {LIVE_MAX_EDGES}"
    )
    if not layout_ok:
        return result(
            "live",
            f"layout: live ({trigger}) — install the [layout] extra "
            "(numpy/scipy) for precomputed static layout",
        )
    return result("static", f"layout: static ({trigger})")


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


def _payload(
    g: nx.MultiDiGraph,
    full: bool,
    top: str | None,
    title: str,
    *,
    comm_of: dict[str, str],
    proj: nx.DiGraph | None,
    positions: dict[str, tuple[int, int]] | None,
    layout_mode: str,
) -> dict[str, Any]:
    domains = _domain_map(g)

    def with_pos(node: dict[str, Any], node_id: str) -> dict[str, Any]:
        # Precomputed coordinates (static tier); the client skips simulation.
        if positions is not None:
            x, y = positions.get(node_id, (0, 0))
            node["x"], node["y"] = x, y
        return node

    if full:
        nodes = [
            with_pos(
                {
                    "id": node_id,
                    "name": data["qualified_name"] or data["name"],
                    "kind": data["kind"].value,
                    "file": data["file"],
                    "line": data["line_span"][0],
                    "domain": domains.get(node_id, ""),
                    "community": comm_of.get(node_id, ""),
                    "unresolved": bool(data["attrs"].get("unresolved")),
                },
                node_id,
            )
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
        assert proj is not None  # supplied by render_html in projection mode
        nodes = [
            with_pos(
                {
                    "id": node_id,
                    "name": data["name"],
                    "kind": data["kind"].value,
                    "file": data["file"],
                    "line": 0,
                    "domain": "",
                    "community": comm_of.get(node_id, ""),
                    "unresolved": data["unresolved"],
                },
                node_id,
            )
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
        "layout": layout_mode,
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
    layout: str = "auto",
    force_inline: bool = False,
) -> RenderResult:
    """Render the graph to a single self-contained HTML file.

    *layout* selects the rendering tier: ``"live"`` keeps the in-browser force
    simulation (the original behavior), ``"static"`` ships precomputed
    coordinates, and ``"auto"`` (default) routes by node/edge count. ``static``
    and ``auto`` fall back to ``live`` when the ``[layout]`` extra is missing.

    Payloads larger than :data:`COMPRESS_OVER_BYTES` (raw JSON) are gzip+base64
    compressed inline and decoded client-side; smaller ones stay plain JSON. The
    *embedded* (post-compression) payload is capped at :data:`MAX_INLINE_BYTES`;
    above it the command raises :class:`ValueError` with guidance (drop
    ``--full``, narrow with ``--top``, or ``export``) unless *force_inline* is
    set, in which case the file is written and the returned note flags the
    over-cap size.

    Returns a :class:`RenderResult` describing the resolved tier. Raises
    :class:`ValueError` when *top* names no module or entity, or when the
    payload exceeds the inline cap and *force_inline* is false.
    """
    if layout not in LAYOUT_MODES:
        raise ValueError(f"layout must be one of {LAYOUT_MODES}, got {layout!r}")

    # Communities (seeded Louvain) drive both subsystem coloring and the
    # community-stacked precomputed layout; compute once and share.
    comm_of: dict[str, str] = {}
    for i, part in enumerate(metrics.communities(g)):
        for node_id in part:
            comm_of[node_id] = str(i)

    # The rendered view decides the routing counts: the projection in default
    # mode, the whole graph in --full.
    proj = None if full else metrics.module_projection(g)
    view: nx.Graph = g if full else proj
    decision = _resolve_layout(
        layout, view.number_of_nodes(), view.number_of_edges(), layout_available()
    )

    positions = compute_layout(view, comm_of) if decision.layout == "static" else None
    # compute_layout returns None if numpy vanished between the check and here;
    # honor that by dropping back to live so the payload stays consistent.
    if decision.layout == "static" and positions is None:
        decision = RenderResult(
            Path(), "live", decision.node_count, decision.edge_count, decision.note
        )

    package = resources.files("hdl_kgraph.viz")
    template = (package / "template.html").read_text(encoding="utf-8")
    d3 = (package / "static" / "d3.v7.min.js").read_text(encoding="utf-8")
    payload = _payload(
        g,
        full,
        top,
        title,
        comm_of=comm_of,
        proj=proj,
        positions=positions,
        layout_mode=decision.layout,
    )
    # Above the threshold, gzip+base64 the payload so large-but-compressible
    # designs still embed (Phase 4b); the client decodes via DecompressionStream.
    # Smaller graphs stay plain JSON so the artifact is human-inspectable.
    raw_bytes = json.dumps(payload).encode("utf-8")
    if len(raw_bytes) > COMPRESS_OVER_BYTES:
        # mtime=0 keeps the gzip header byte-identical across runs (determinism).
        gz = gzip.compress(raw_bytes, compresslevel=9, mtime=0)
        encoding = "gzip+base64"
        embedded = base64.b64encode(gz).decode("ascii")  # base64 has no "</"
    else:
        encoding = "json"
        # "</" must not appear verbatim inside an inline <script> payload.
        embedded = raw_bytes.decode("utf-8").replace("</", "<\\/")

    # The guard measures the *embedded* size before writing anything: a huge
    # design would otherwise emit an HTML file no browser can open. Compression
    # directly widens what fits, so the embedded bytes are what must clear it.
    size = len(embedded.encode("utf-8"))
    if size > MAX_INLINE_BYTES and not force_inline:
        raise ValueError(
            f"graph payload is {size / 1e6:.0f} MB, over the "
            f"{MAX_INLINE_BYTES / 1e6:.0f} MB inline limit — drop --full, "
            f"narrow with --top, use `hdl-kgraph export`, or pass "
            f"--force-inline to write it anyway"
        )
    decision.compressed = encoding == "gzip+base64"
    if size > MAX_INLINE_BYTES:
        decision.note = (
            f"payload {size / 1e6:.0f} MB exceeds the "
            f"{MAX_INLINE_BYTES / 1e6:.0f} MB inline limit (--force-inline)"
        )
    elif decision.compressed:
        decision.note = (
            f"payload gzip-compressed: {len(raw_bytes) / 1e6:.0f} MB raw → "
            f"{size / 1e6:.0f} MB inlined"
        )
    html = (
        template.replace(_D3_MARKER, d3, 1)
        .replace(_ENC_MARKER, encoding, 1)
        .replace(_DATA_MARKER, embedded, 1)
    )
    out_path = Path(out_path)
    out_path.write_text(html, encoding="utf-8")
    decision.path = out_path
    return decision
