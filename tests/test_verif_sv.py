"""SV verification-construct tests (M5): assertions, covergroups, clocking."""

from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, NodeKind


@pytest.fixture(scope="module")
def ir(fixtures_dir: Path):
    return SystemVerilogParser().parse(
        Path("verif_constructs.sv"), (fixtures_dir / "verif_constructs.sv").read_text()
    )


@pytest.fixture(scope="module")
def graph(ir):
    return build_graph([ir])


def test_grammar_covers_the_constructs(ir) -> None:
    # The de-risk gate: if the grammar mis-parses assertion/covergroup
    # syntax, everything below is built on sand.
    assert ir.parse_error_count == 0


def _nodes(graph, kind):
    return {n: d for n, d in graph.nodes(data=True) if d["kind"] is kind}


def test_property_and_sequence_nodes(graph) -> None:
    assert "verif_constructs.sv::property:verif_dut.p_handshake" in graph
    assert "verif_constructs.sv::sequence:verif_dut.s_pulse" in graph


def test_assertion_nodes_with_statement_flavors(graph) -> None:
    assertions = _nodes(graph, NodeKind.ASSERTION)
    by_name = {d["name"]: d for d in assertions.values()}
    assert by_name["a_handshake"]["attrs"]["statement"] == "assert"
    assert by_name["c_pulse"]["attrs"]["statement"] == "cover"
    assert by_name["m_no_grant_idle"]["attrs"]["statement"] == "assume"
    unnamed = [n for n in by_name if n.startswith("assert@")]
    assert len(unnamed) == 1  # the label-less assert property


def test_labeled_assert_resolves_to_property(graph) -> None:
    edge = next(
        d
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.ASSERTS_ON
        and u.endswith("verif_dut.a_handshake")
        and v.endswith("property:verif_dut.p_handshake")
    )
    assert edge["confidence"] == 1.0


def test_property_asserts_on_its_signals(graph) -> None:
    targets = {
        v
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.ASSERTS_ON and u.endswith("verif_dut.p_handshake")
    }
    assert "verif_constructs.sv::port:verif_dut.req" in targets
    assert "verif_constructs.sv::port:verif_dut.gnt" in targets
    assert "verif_constructs.sv::port:verif_dut.rst_n" in targets  # disable iff


def test_covergroup_and_coverpoints(graph) -> None:
    assert "verif_constructs.sv::covergroup:verif_dut.cg_bus" in graph
    points = _nodes(graph, NodeKind.COVERPOINT)
    names = {d["name"] for d in points.values()}
    assert "cp_data" in names
    assert any(n.startswith("cp@") for n in names)  # the unlabeled coverpoint
    covers = [
        (u, v)
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.COVERS and u.endswith("cg_bus.cp_data")
    ]
    assert covers == [
        ("verif_constructs.sv::coverpoint:verif_dut.cg_bus.cp_data",
         "verif_constructs.sv::port:verif_dut.data")
    ]


def test_covergroup_clocked_by_its_event(graph) -> None:
    assert any(
        d["kind"] is EdgeKind.CLOCKED_BY
        and u.endswith("covergroup:verif_dut.cg_bus")
        and v.endswith("port:verif_dut.clk")
        for u, v, d in graph.edges(data=True)
    )


def test_default_clocking_block(graph) -> None:
    cb = graph.nodes["verif_constructs.sv::clocking_block:verif_dut.cb"]
    assert cb["attrs"]["is_default"] is True
    assert any(
        d["kind"] is EdgeKind.CLOCKED_BY and u.endswith("clocking_block:verif_dut.cb")
        for u, v, d in graph.edges(data=True)
    )


def test_class_constraints(graph) -> None:
    constraints = _nodes(graph, NodeKind.CONSTRAINT)
    names = {d["qualified_name"] for d in constraints.values()}
    assert names == {"verif_item.c_addr", "verif_item.c_burst"}
