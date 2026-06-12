"""Visualization tests (M5): self-contained HTML output."""

import json
from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.viz import render_html


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
    out = render_html(graph, tmp_path / "g.html", title="t")
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    # Self-containment: no external script/style/link references.
    assert 'src="http' not in html and "src='http" not in html
    assert "<link" not in html
    assert "d3js.org v7" in html  # the vendored bundle is inlined


def test_default_payload_is_the_module_projection(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html").read_text()
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
    html = render_html(graph, tmp_path / "g.html", full=True).read_text()
    payload = _embedded_payload(html)
    kinds = {n["kind"] for n in payload["nodes"]}
    assert "signal" in kinds and "process" in kinds
    domains = {n["domain"] for n in payload["nodes"] if n["domain"]}
    assert domains  # signals/processes carry their domain for the tooltip
    assert "domain-filters" not in html  # the filter section was retired


def test_hierarchy_tree_embedded(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html", top="df_top").read_text()
    payload = _embedded_payload(html)
    assert len(payload["hierarchy"]) == 1
    root = payload["hierarchy"][0]
    assert root["module"] == "df_top"
    assert any(child["module"] == "df_sub" for child in root["children"])


def test_template_canvas_sizing_survives_embedded_viewers(graph, tmp_path: Path) -> None:
    # Embedded/iframe viewers can report devicePixelRatio 0 or zero client
    # sizes; sizing the bitmap from those values blanks the graph view while
    # the hierarchy (plain DOM) keeps working.
    html = render_html(graph, tmp_path / "g.html").read_text()
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
    html = render_html(graph, tmp_path / "g.html").read_text()
    payload = _embedded_payload(html)
    assert len(payload["communities"]) >= 2
    community = {n["name"]: n["community"] for n in payload["nodes"]}
    assert community["df_top"] == community["df_sub"]
    assert community["two_clock_top"] == community["cdc_child"]
    assert community["df_top"] != community["two_clock_top"]
    assert all(n["community"] in payload["communities"] for n in payload["nodes"])


def test_template_has_recenter_control(graph, tmp_path: Path) -> None:
    html = render_html(graph, tmp_path / "g.html").read_text()
    assert 'id="recenter"' in html
    assert "function fitView" in html
    assert "call(zoom.transform, t)" in html


def test_payload_json_is_parseable_with_funny_names(graph, tmp_path: Path) -> None:
    # The "</" escaping path: just make sure the embedded JSON survives.
    html = render_html(graph, tmp_path / "g.html", full=True).read_text()
    payload = _embedded_payload(html)
    assert payload["nodes"] and payload["links"]
