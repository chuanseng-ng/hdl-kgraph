"""Impact-radius tests (M4): who breaks when a unit or file changes."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import analysis
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.preprocessor import PreprocEmitter, Preprocessor
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import EdgeKind, NodeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    irs = []
    for path in sorted(fixtures_dir.iterdir()):
        if path.suffix in sv.suffixes:
            irs.append(sv.parse(Path(path.name), path.read_text()))
        elif path.suffix in vhdl.suffixes:
            irs.append(vhdl.parse(Path(path.name), path.read_text()))
    return build_graph(irs)


def _names(records, kind=None):
    return {r.name for r in records if kind is None or r.kind is kind}


def test_module_impact_flags_instantiating_parents(graph) -> None:
    seeds = ["simple_counter.sv::module:simple_counter"]
    records = analysis.impact_radius(graph, seeds)
    names = _names(records, NodeKind.MODULE)
    assert "top" in names  # top.v instantiates simple_counter
    by_name = {r.name: r for r in records}
    assert by_name["top"].via is EdgeKind.INSTANTIATES
    assert by_name["top"].depth == 1


def test_package_impact_flags_importers(graph) -> None:
    seeds = ["my_pkg.sv::package:my_pkg"]
    records = analysis.impact_radius(graph, seeds)
    assert "imports_pkg" in _names(records, NodeKind.MODULE)
    assert any(r.via is EdgeKind.IMPORTS for r in records)


def test_class_impact_flags_subclasses(graph) -> None:
    seeds = [
        node_id
        for node_id, data in graph.nodes(data=True)
        if data["kind"] is NodeKind.CLASS and data["name"] == "base_item"
    ]
    assert seeds
    records = analysis.impact_radius(graph, seeds)
    extended = {r.name for r in records if r.via is EdgeKind.EXTENDS}
    assert "burst_item" in extended  # classes.sv: burst_item extends base_item


def test_vhdl_entity_impact_crosses_architecture_to_instantiators(graph) -> None:
    seeds = ["alu.vhd::entity:alu"]
    records = analysis.impact_radius(graph, seeds)
    names = _names(records)
    assert "vhdl_top" in names  # instantiates alu directly
    # alu's own architecture is affected by an entity change too
    assert any(r.kind is NodeKind.ARCHITECTURE for r in records)


def test_vhdl_package_impact_flags_users(graph) -> None:
    seeds = ["util_pkg.vhd::vhdl_package:util_pkg"]
    records = analysis.impact_radius(graph, seeds)
    assert any(r.via is EdgeKind.USES_PACKAGE for r in records)


def test_max_depth_truncates(graph) -> None:
    seeds = ["simple_counter.sv::module:simple_counter"]
    unlimited = analysis.impact_radius(graph, seeds)
    limited = analysis.impact_radius(graph, seeds, max_depth=1)
    assert {r.depth for r in limited} <= {1}
    assert len(limited) <= len(unlimited)


def test_header_impact_flags_includers(tmp_path: Path) -> None:
    (tmp_path / "defs.svh").write_text("`define WIDTH 8\n")
    (tmp_path / "leaf.sv").write_text(
        '`include "defs.svh"\nmodule leaf(output logic [`WIDTH-1:0] y);\nendmodule\n'
    )
    (tmp_path / "top.sv").write_text("module top;\n  leaf u_leaf();\nendmodule\n")
    preprocessor = Preprocessor(base=tmp_path)
    parser = SystemVerilogParser()
    irs = []
    for name in ("defs.svh", "leaf.sv", "top.sv"):
        pp = preprocessor.preprocess(tmp_path / name)
        ir = parser.parse(Path(name), pp.text, line_map=pp.line_map)
        PreprocEmitter().emit(pp, ir)
        irs.append(ir)
    graph = build_graph(irs)

    records = analysis.impact_radius(graph, ["file:defs.svh"])
    files = {r.name for r in records if r.kind is NodeKind.FILE}
    assert "leaf.sv" in files  # includer
    modules = _names(records, NodeKind.MODULE)
    assert {"leaf", "top"} <= modules  # declared in the includer, then upward
