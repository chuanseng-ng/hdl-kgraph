"""Pass-2 linker tests for VHDL and mixed-language resolution (M3)."""

from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.graph.analysis import hierarchy_tree, instances_of
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.ids import stub_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import EdgeKind, NodeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path) -> nx.MultiDiGraph:
    """The fixture corpus parsed with both backends, then linked."""
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    irs: list[FileIR] = []
    for path in sorted(fixtures_dir.iterdir()):
        if path.suffix in sv.suffixes:
            irs.append(sv.parse(Path(path.name), path.read_text()))
        elif path.suffix in vhdl.suffixes:
            irs.append(vhdl.parse(Path(path.name), path.read_text()))
    return build_graph(irs)


def edges_between(g: nx.MultiDiGraph, src: str, dst: str, kind: EdgeKind) -> list[dict]:
    if not g.has_edge(src, dst):
        return []
    return [d for d in g[src][dst].values() if d["kind"] is kind]


def test_implements_resolves_architecture_to_entity(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph, "alu.vhd::architecture:rtl", "alu.vhd::entity:alu", EdgeKind.IMPLEMENTS
    )
    assert edge["confidence"] == 1.0


def test_sv_instantiates_vhdl_entity_at_0_8(graph: nx.MultiDiGraph) -> None:
    """Verilog-top / VHDL-leaf: cross-language name match, capped at 0.8."""
    (edge,) = edges_between(
        graph,
        "mixed_sv_top.sv::instance:mixed_sv_top.u_alu",
        "alu.vhd::entity:alu",
        EdgeKind.INSTANTIATES,
    )
    assert edge["confidence"] == 0.8


def test_vhdl_component_binds_to_sv_module_at_0_8(graph: nx.MultiDiGraph) -> None:
    """VHDL-top / Verilog-leaf via default binding of a component."""
    (edge,) = edges_between(
        graph,
        "vhdl_top.vhd::instance:rtl.u_counter",
        "simple_counter.sv::module:simple_counter",
        EdgeKind.INSTANTIATES,
    )
    assert edge["confidence"] == 0.8


def test_case_insensitive_cross_language_match(graph: nx.MultiDiGraph) -> None:
    """The lowercase VHDL `fifo` component finds the SV `FIFO` module."""
    (edge,) = edges_between(
        graph,
        "vhdl_top.vhd::instance:rtl.u_fifo",
        "fifo_case.sv::module:FIFO",
        EdgeKind.INSTANTIATES,
    )
    assert edge["confidence"] == 0.8


def test_cross_language_connects_match_ports_case_insensitively(
    graph: nx.MultiDiGraph,
) -> None:
    (edge,) = edges_between(
        graph,
        "vhdl_top.vhd::instance:rtl.u_counter",
        "simple_counter.sv::port:simple_counter.count",
        EdgeKind.CONNECTS,
    )
    assert edge["attrs"]["port_name"] == "count"
    (param_edge,) = edges_between(
        graph,
        "vhdl_top.vhd::instance:rtl.u_counter",
        "simple_counter.sv::parameter:simple_counter.WIDTH",
        EdgeKind.PARAMETERIZES,
    )
    assert param_edge["attrs"]["value_text"] == "8"


def test_direct_entity_instantiation(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "vhdl_top.vhd::instance:rtl.u_alu",
        "alu.vhd::entity:alu",
        EdgeKind.INSTANTIATES,
    )
    # Cross-file VHDL->VHDL unique match.
    assert edge["confidence"] == 0.8
    assert edge["attrs"]["architecture"] == "rtl"


def test_configuration_overrides_default_binding(graph: nx.MultiDiGraph) -> None:
    """The acceptance-critical case: cfg_top_special rebinds u_leaf."""
    src = "cfg_override.vhd::instance:rtl.u_leaf"
    targets = {
        v: d for _, v, d in graph.out_edges(src, data=True) if d["kind"] is EdgeKind.INSTANTIATES
    }
    # Only the bound entity gets the edge; the like-named default does not.
    assert set(targets) == {"cfg_override.vhd::entity:leaf_special"}
    edge = targets["cfg_override.vhd::entity:leaf_special"]
    assert edge["attrs"]["bound_by"] == "cfg_override.vhd::configuration:cfg_top_special"


def test_configures_binds_edge(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "cfg_override.vhd::configuration:cfg_top_special",
        "cfg_override.vhd::entity:cfg_top",
        EdgeKind.BINDS,
    )
    assert edge["attrs"]["role"] == "configures"


def test_uses_package_resolves_to_local_package(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "util_pkg.vhd::entity:pkg_user",
        "util_pkg.vhd::vhdl_package:util_pkg",
        EdgeKind.USES_PACKAGE,
    )
    assert edge["confidence"] == 1.0  # same file


def test_ieee_packages_become_library_qualified_stubs(graph: nx.MultiDiGraph) -> None:
    stub_id = stub_node_id(NodeKind.VHDL_PACKAGE, "ieee.std_logic_1164")
    assert graph.nodes[stub_id]["attrs"]["unresolved"] is True


def test_mixed_hierarchy_is_connected_both_ways(graph: nx.MultiDiGraph) -> None:
    sv_top = hierarchy_tree(graph, "mixed_sv_top.sv::module:mixed_sv_top")
    assert [c.module_name for c in sv_top.children] == ["alu"]
    assert sv_top.children[0].architecture == "rtl"

    vhdl_top = hierarchy_tree(graph, "vhdl_top.vhd::entity:vhdl_top")
    assert vhdl_top.architecture == "rtl"
    assert sorted(c.module_name for c in vhdl_top.children) == [
        "FIFO",
        "alu",
        "simple_counter",
    ]


def test_instances_of_spans_languages(graph: nx.MultiDiGraph) -> None:
    records = instances_of(graph, "alu")
    files = {r["file"] for r in records}
    assert files == {"mixed_sv_top.sv", "vhdl_top.vhd"}
    # Case-insensitive lookup for the VHDL entity.
    assert {r["file"] for r in instances_of(graph, "ALU")} == files
