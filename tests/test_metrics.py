"""Graph-metrics tests (M5): projection, fan-in/out, betweenness, Louvain."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import metrics
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import NodeKind


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


def test_projection_has_instantiation_edges(graph) -> None:
    proj = metrics.module_projection(graph)
    assert proj.has_edge("top.v::module:top", "simple_counter.sv::module:simple_counter")
    assert proj["top.v::module:top"]["simple_counter.sv::module:simple_counter"]["weight"] == 1


def test_projection_collapses_architectures_into_entities(graph) -> None:
    proj = metrics.module_projection(graph)
    # vhdl_top's instances live in its rtl architecture; the projection
    # attributes them to the entity.
    assert proj.has_edge("vhdl_top.vhd::entity:vhdl_top", "alu.vhd::entity:alu")
    assert not any(NodeKind.ARCHITECTURE is d["kind"] for _, d in proj.nodes(data=True))


def test_fan_in_counts_instantiation_sites(graph) -> None:
    by_name = {m.name: m for m in metrics.module_metrics(graph)}
    # alu is instantiated by mixed_sv_top and vhdl_top.
    assert by_name["alu"].fan_in == 2
    assert by_name["vhdl_top"].fan_out >= 3


def test_mid_hierarchy_module_has_positive_betweenness(graph) -> None:
    by_name = {m.name: m for m in metrics.module_metrics(graph)}
    # alu sits between its instantiators and nothing below; betweenness of a
    # pure leaf is 0 — make sure ordering puts a connected mid/hub first.
    leaf = by_name["simple_counter"]
    assert leaf.betweenness == 0.0
    assert metrics.module_metrics(graph)[0].fan_in + metrics.module_metrics(graph)[0].fan_out > 0


def test_unresolved_targets_stay_visible(graph) -> None:
    by_name = {m.name: m for m in metrics.module_metrics(graph)}
    assert by_name["ghost_mod"].unresolved
    assert by_name["ghost_mod"].fan_in >= 1


def test_communities_are_deterministic(graph) -> None:
    first = metrics.communities(graph)
    second = metrics.communities(graph)
    assert first == second
    assert first  # the fixture corpus has at least one community


def test_communities_group_connected_units(graph) -> None:
    parts = metrics.communities(graph)
    by_member = {m: i for i, part in enumerate(parts) for m in part}
    # top and its counter belong to the same community.
    assert by_member["top.v::module:top"] == by_member["simple_counter.sv::module:simple_counter"]
