"""Visualization tests (M5): self-contained HTML output.

Phase 1/2 of the viz-scalability work (docs/viz-scalability.md) adds renderer
hygiene and a precomputed-layout tier; the small fixture graphs here stay in
the unchanged "live" tier, so the original asserts hold by construction.
"""

import base64
import gzip
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


def _embedded_data(html: str) -> tuple[str, str]:
    """Return ``(encoding, body)`` of the embedded ``#graph-data`` script."""
    start = html.index('<script id="graph-data"')
    gt = html.index(">", start) + 1
    end = html.index("</script>", gt)
    open_tag = html[start:gt]
    encoding = "gzip+base64" if 'data-encoding="gzip+base64"' in open_tag else "json"
    return encoding, html[gt:end]


def _embedded_payload(html: str) -> dict:
    encoding, body = _embedded_data(html)
    if encoding == "gzip+base64":
        return json.loads(gzip.decompress(base64.b64decode(body)))
    return json.loads(body)


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


# ---------------------------------------------------------------------------
# viz-scalability Phase 4a: inline-payload size guard.
# ---------------------------------------------------------------------------


def test_oversized_payload_is_refused(graph, tmp_path: Path, monkeypatch) -> None:
    # Shrink the cap instead of building a giant graph: any real payload trips
    # it, and the error must point the user at the escapes.
    monkeypatch.setattr("hdl_kgraph.viz.MAX_INLINE_BYTES", 10)
    with pytest.raises(ValueError, match="inline limit") as exc:
        render_html(graph, tmp_path / "g.html")
    message = str(exc.value)
    assert "--force-inline" in message and "--full" in message
    assert "export" in message  # Phase 5 escape hatch is one of the suggestions
    assert not (tmp_path / "g.html").exists()  # nothing written on refusal


def test_force_inline_overrides_the_cap(graph, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("hdl_kgraph.viz.MAX_INLINE_BYTES", 10)
    result = render_html(graph, tmp_path / "g.html", force_inline=True)
    assert result.path.is_file()
    assert "force-inline" in result.note and "exceeds" in result.note
    # The artifact is still well-formed despite the override.
    assert _embedded_payload(result.path.read_text())["nodes"]


def test_small_graph_under_default_cap_has_no_note(graph, tmp_path: Path) -> None:
    # Regression: the default cap never trips a normal design.
    result = render_html(graph, tmp_path / "g.html")
    assert result.note == ""


# ---------------------------------------------------------------------------
# viz-scalability Phase 4b: inline-payload gzip compression.
# ---------------------------------------------------------------------------


def test_small_payload_stays_plain_json(graph, tmp_path: Path) -> None:
    # Under the (default) threshold the payload stays human-inspectable JSON.
    result = render_html(graph, tmp_path / "g.html")
    assert result.compressed is False
    encoding, _ = _embedded_data(result.path.read_text())
    assert encoding == "json"
    assert _embedded_payload(result.path.read_text())["nodes"]  # plain parse works


def test_large_payload_is_gzip_compressed(graph, tmp_path: Path, monkeypatch) -> None:
    # Shrink the threshold instead of building a huge graph: any real payload
    # then takes the gzip+base64 path and must round-trip back to the same data.
    monkeypatch.setattr("hdl_kgraph.viz.COMPRESS_OVER_BYTES", 1)
    result = render_html(graph, tmp_path / "g.html")
    assert result.compressed is True
    assert "gzip-compressed" in result.note
    html = result.path.read_text()
    encoding, _ = _embedded_data(html)
    assert encoding == "gzip+base64"
    payload = _embedded_payload(html)  # base64 -> gunzip -> json
    assert {"df_top", "df_sub"} <= {n["name"] for n in payload["nodes"]}


def test_template_has_decompression_branch(graph, tmp_path: Path) -> None:
    # The client-side async decode path must always be present in the template,
    # regardless of which encoding a given render uses.
    html = render_html(graph, tmp_path / "g.html").path.read_text()
    assert "DecompressionStream" in html
    assert "async function loadGraphData" in html


def test_compressed_render_is_deterministic(graph, tmp_path: Path, monkeypatch) -> None:
    # gzip mtime=0 keeps the compressed bytes byte-identical across runs.
    monkeypatch.setattr("hdl_kgraph.viz.COMPRESS_OVER_BYTES", 1)
    a = render_html(graph, tmp_path / "a.html").path.read_text()
    b = render_html(graph, tmp_path / "b.html").path.read_text()
    assert a == b


# ---------------------------------------------------------------------------
# viz-scalability Phase 3: collapsed community view (aggregation / drill-down).
# ---------------------------------------------------------------------------


def test_collapse_payload_carries_supernodes(graph, tmp_path: Path) -> None:
    result = render_html(graph, tmp_path / "g.html", collapse=True)
    payload = _embedded_payload(result.path.read_text())
    assert payload["collapse"] is True
    assert payload["supernodes"] and "label" in payload["supernodes"][0]
    # One supernode per community, keyed by the community label.
    assert {s["id"] for s in payload["supernodes"]} == set(payload["communities"])
    assert "superlinks" in payload


def test_non_collapse_render_has_no_supernodes(graph, tmp_path: Path) -> None:
    payload = _embedded_payload(render_html(graph, tmp_path / "g.html").path.read_text())
    assert payload["collapse"] is False
    assert "supernodes" not in payload


def test_template_has_collapse_drilldown(graph, tmp_path: Path) -> None:
    # No JS harness in the repo, so pin the expand/collapse branch by structure.
    html = render_html(graph, tmp_path / "g.html", collapse=True).path.read_text()
    assert "function rebuild()" in html
    assert "dblclick" in html and "expanded" in html


def test_template_has_search_auto_expand(graph, tmp_path: Path) -> None:
    # Search must reveal hits hidden inside collapsed communities (pinned by
    # structure, like the other template behaviors).
    html = render_html(graph, tmp_path / "g.html", collapse=True).path.read_text()
    assert "syncSearchExpansion" in html and "searchExpanded" in html


def test_collapse_rejects_full(graph, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full"):
        render_html(graph, tmp_path / "g.html", collapse=True, full=True)
    assert not (tmp_path / "g.html").exists()


def test_compression_lets_oversized_payload_embed(graph, tmp_path: Path, monkeypatch) -> None:
    # The guard measures the *embedded* (post-compression) size, so a payload
    # that would be refused raw can still embed once gzipped.
    monkeypatch.setattr("hdl_kgraph.viz.COMPRESS_OVER_BYTES", 10**12)  # force plain JSON
    raw_html = render_html(graph, tmp_path / "raw.html", full=True).path.read_text()
    raw_size = len(_embedded_data(raw_html)[1])

    monkeypatch.setattr("hdl_kgraph.viz.COMPRESS_OVER_BYTES", 1)  # force compression
    comp = render_html(graph, tmp_path / "comp.html", full=True)
    comp_size = len(_embedded_data(comp.path.read_text())[1])
    assert comp.compressed and comp_size < raw_size  # gzip actually shrank it

    # Cap below the raw size but at/above the compressed size: the raw JSON
    # would be refused, but the gzip payload fits and is written.
    monkeypatch.setattr("hdl_kgraph.viz.MAX_INLINE_BYTES", raw_size - 1)
    ok = render_html(graph, tmp_path / "ok.html", full=True)
    assert ok.path.is_file() and ok.compressed

    # Same cap with compression disabled -> refused, proving compression is what
    # let it through.
    monkeypatch.setattr("hdl_kgraph.viz.COMPRESS_OVER_BYTES", 10**12)
    with pytest.raises(ValueError, match="inline limit"):
        render_html(graph, tmp_path / "no.html", full=True)
