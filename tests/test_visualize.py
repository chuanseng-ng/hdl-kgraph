"""Visualization tests (M5): self-contained HTML output.

Phase 1/2 of the viz-scalability work (docs/viz-scalability.md) adds renderer
hygiene and a precomputed-layout tier; the small fixture graphs here stay in
the unchanged "live" tier, so the original asserts hold by construction.
"""

import json
from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.viz import (
    LIVE_MAX_EDGES,
    LIVE_MAX_NODES,
    STATIC_MAX_NODES,
    _resolve_layout,
    render_html,
)
from hdl_kgraph.viz.layout import compute_layout


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    irs = [
        sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        sv.parse(Path("two_clock_cdc.sv"), (fixtures_dir / "two_clock_cdc.sv").read_text()),
    ]
    return build_graph(irs)


def _embedded_payload(html: str) -> dict:
    marker = '<script id="graph-data" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return json.loads(html[start:end])


def test_render_writes_self_contained_html(graph, tmp_path: Path) -> None:
    result = render_html(graph, tmp_path / "g.html", title="t")
    html = result.path.read_text()
    assert html.startswith("<!DOCTYPE html>")
    # Self-containment: no external script/style/link references.
    assert 'src="http' not in html and "src='http" not in html
    assert "<link" not in html
    assert "d3js.org v7" in html  # the vendored bundle is inlined


def test_default_payload_is_the_module_projection(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    payload = _embedded_payload(html)
    kinds = {n["kind"] for n in payload["nodes"]}
    assert kinds == {"module"}
    names = {n["name"] for n in payload["nodes"]}
    assert {"df_top", "df_sub", "two_clock_top", "cdc_child"} <= names
    assert any(
        link["source"].endswith("module:df_top") and link["target"].endswith("module:df_sub")
        for link in payload["links"]
    )


def test_full_payload_includes_signals_and_domains(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html", full=True).path.read_text()
    payload = _embedded_payload(html)
    kinds = {n["kind"] for n in payload["nodes"]}
    assert "signal" in kinds and "process" in kinds
    domains = {n["domain"] for n in payload["nodes"] if n["domain"]}
    assert domains  # signals/processes carry their domain for the tooltip
    assert "domain-filters" not in html  # the filter section was retired


def test_hierarchy_tree_embedded(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html", top="df_top").path.read_text()
    payload = _embedded_payload(html)
    assert len(payload["hierarchy"]) == 1
    root = payload["hierarchy"][0]
    assert root["module"] == "df_top"
    assert any(child["module"] == "df_sub" for child in root["children"])


def test_template_canvas_sizing_survives_embedded_viewers(graph, tmp_path: Path) -> None:
    # Embedded/iframe viewers can report devicePixelRatio 0 or zero client
    # sizes; sizing the bitmap from those values blanks the graph view while
    # the hierarchy (plain DOM) keeps working.
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    assert "window.devicePixelRatio || 1" in html
    # The canvas must keep its layout size while hidden (visibility toggle):
    # a display:none canvas reads 0x0 on the first switch to the graph tab.
    assert "canvas.style.display" not in html
    assert "canvas.style.visibility" in html
    # The bitmap must be sized from (and the ResizeObserver attached to) the
    # #view container, never the canvas itself: in engines where the canvas's
    # layout follows its bitmap (e.g. no `inset` support), measuring the
    # canvas feeds back into the bitmap and grows it ~1.5x per observer round
    # until the renderer crashes.
    assert "inset:" not in html.split("</style>")[0]
    assert '.observe(document.getElementById("view"))' in html
    assert ".observe(canvas)" not in html
    assert "canvas.clientWidth" not in html
    # Deferred-layout viewers read 0x0 at first: the retry must exist and be
    # bounded so a permanently hidden viewer can't spin an rAF chain forever.
    assert "requestAnimationFrame(resize)" in html
    assert "resizeRetries++ < 120" in html


def test_payload_carries_communities(graph, tmp_path: Path) -> None:
    # The two fixture files are disconnected subsystems, so Louvain yields
    # at least two communities; connected units share one.
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    payload = _embedded_payload(html)
    assert len(payload["communities"]) >= 2
    community = {n["name"]: n["community"] for n in payload["nodes"]}
    assert community["df_top"] == community["df_sub"]
    assert community["two_clock_top"] == community["cdc_child"]
    assert community["df_top"] != community["two_clock_top"]
    assert all(n["community"] in payload["communities"] for n in payload["nodes"])


def test_template_has_recenter_control(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    assert 'id="recenter"' in html
    assert "function fitView" in html
    assert "call(zoom.transform, t)" in html


def test_payload_json_is_parseable_with_funny_names(graph, tmp_path: Path) -> None:
    # The "</" escaping path: just make sure the embedded JSON survives.
    html = render_html(graph, tmp_path / "g.html", full=True).path.read_text()
    payload = _embedded_payload(html)
    assert payload["nodes"] and payload["links"]


# --------------------------------------------------------------------------
# viz-scalability Phase 1 (renderer hygiene) + Phase 2 (precomputed layout)
# --------------------------------------------------------------------------


def test_template_has_render_hygiene_and_static_branch(graph, tmp_path: Path) -> None:
    # Phase 1 draw-loop budgets and Phase 2 static/quadtree branch must be
    # present in the template (no JS test harness in the repo, so pin by
    # structure like the canvas-sizing asserts above).
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    assert "MAX_LABELS" in html and "MAX_DRAWN_EDGES" in html  # per-frame budgets
    assert "new Path2D(" in html  # edges batched, not stroked one-by-one
    assert "showing " in html  # the "showing X of Y edges" sampling note
    assert "const STATIC = DATA.layout" in html  # static tier branch
    assert "d3.quadtree(" in html  # static-tier picking


def test_small_graph_stays_live_with_no_coordinates(graph, tmp_path: Path) -> None:
    # The no-regression guarantee: a small design stays in the live tier with
    # no precomputed coordinates, so the in-browser simulation runs as before.
    result = render_html(graph, tmp_path / "g.html")
    assert result.layout == "live"
    payload = _embedded_payload(result.path.read_text())
    assert payload["layout"] == "live"
    assert all("x" not in n and "y" not in n for n in payload["nodes"])
    assert result.node_count <= LIVE_MAX_NODES


def test_resolve_layout_routes_by_size_and_extra() -> None:
    small = (LIVE_MAX_NODES - 1, LIVE_MAX_EDGES - 1)
    big = (LIVE_MAX_NODES + 1, LIVE_MAX_EDGES + 1)

    # auto: small stays live, large goes static when the extra is available.
    assert _resolve_layout("auto", *small, True).layout == "live"
    assert _resolve_layout("auto", *big, True).layout == "static"
    # auto over the threshold without the extra: fall back to live, and say so.
    fallback = _resolve_layout("auto", *big, False)
    assert fallback.layout == "live"
    assert "[layout] extra" in fallback.note
    # explicit live never computes a layout.
    assert _resolve_layout("live", *big, True).layout == "live"
    # explicit static without the extra falls back to live with a note.
    static_no_extra = _resolve_layout("static", *small, False)
    assert static_no_extra.layout == "live"
    assert "[layout] extra" in static_no_extra.note
    # over the static budget still renders static, with a heads-up note.
    huge = _resolve_layout("static", STATIC_MAX_NODES + 1, 10, True)
    assert huge.layout == "static" and "budget" in huge.note


def test_static_request_falls_back_to_live_without_numpy(
    graph, tmp_path: Path, monkeypatch
) -> None:
    # When the [layout] extra is absent the command must not fail: it quietly
    # renders the live tier and reports why.
    monkeypatch.setattr("hdl_kgraph.viz.layout_available", lambda: False)
    result = render_html(graph, tmp_path / "g.html", layout="static")
    assert result.layout == "live"
    assert "[layout] extra" in result.note
    payload = _embedded_payload(result.path.read_text())
    assert payload["layout"] == "live"
    assert all("x" not in n for n in payload["nodes"])


def test_static_layout_embeds_integer_coordinates(graph, tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    result = render_html(graph, tmp_path / "g.html", layout="static")
    assert result.layout == "static"
    payload = _embedded_payload(result.path.read_text())
    assert payload["layout"] == "static"
    assert payload["nodes"]
    for n in payload["nodes"]:
        assert isinstance(n["x"], int) and isinstance(n["y"], int)


def test_static_layout_is_deterministic(graph, tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    a = render_html(graph, tmp_path / "a.html", layout="static").path.read_text()
    b = render_html(graph, tmp_path / "b.html", layout="static").path.read_text()
    assert a == b  # seeded Louvain + seeded layout => byte-identical artifact


def test_compute_layout_covers_all_nodes_at_scale() -> None:
    # Scale smoke without parsing: a synthetic graph must get integer coords
    # for every node, deterministically. Guarded by the [layout] extra.
    pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    g = nx.barabasi_albert_graph(2000, 2, seed=1)
    g = nx.relabel_nodes(g, {i: f"n{i}" for i in g.nodes()})
    # Assign a handful of synthetic communities so the stacking path is hit.
    comm_of = {nid: str(i % 8) for i, nid in enumerate(g.nodes())}
    pos = compute_layout(g, comm_of)
    assert pos is not None
    assert set(pos) == set(g.nodes())
    assert all(isinstance(x, int) and isinstance(y, int) for x, y in pos.values())
    assert compute_layout(g, comm_of) == pos  # deterministic
