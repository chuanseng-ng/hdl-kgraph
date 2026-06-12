"""port_map analysis tests (M6): ports/params in order, instance bindings."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import analysis
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import Language, NodeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    names = ["simple_counter.sv", "top_positional.v", "adder.v", "dataflow.sv"]
    irs = [sv.parse(Path(n), (fixtures_dir / n).read_text()) for n in names]
    irs.append(vhdl.parse(Path("alu.vhd"), (fixtures_dir / "alu.vhd").read_text()))
    return build_graph(irs)


def test_ports_in_declaration_order_with_directions(graph) -> None:
    (unit,) = analysis.port_map(graph, "simple_counter")
    assert unit["kind"] is NodeKind.MODULE
    assert [p["name"] for p in unit["ports"]] == ["clk", "rst_n", "en", "count"]
    assert [p["direction"] for p in unit["ports"]] == ["input"] * 3 + ["output"]
    assert [p["name"] for p in unit["parameters"]] == ["WIDTH"]
    assert unit["parameters"][0]["is_localparam"] is False


def test_vhdl_generics_and_case_insensitive_lookup(graph) -> None:
    (unit,) = analysis.port_map(graph, "ALU")
    assert unit["kind"] is NodeKind.ENTITY
    assert unit["language"] is Language.VHDL
    assert [p["name"] for p in unit["ports"]] == ["a", "b", "op", "result"]
    assert [p["name"] for p in unit["parameters"]] == ["width"]


def test_positional_instance_bindings(graph) -> None:
    (unit,) = analysis.port_map(graph, "adder", instance="u_adder")
    (inst,) = unit["instances"]
    assert inst["instance_name"] == "u_adder"
    bindings = inst["bindings"]
    assert [b["position"] for b in bindings] == list(range(len(bindings)))
    assert all(b["port"] for b in bindings)  # positions resolved to port names


def test_named_instance_bindings(graph) -> None:
    (unit,) = analysis.port_map(graph, "df_sub", instance="u_sub")
    (inst,) = unit["instances"]
    by_port = {b["port"]: b for b in inst["bindings"]}
    assert by_port["clk"]["actual"] == "clk"
    assert by_port["i"]["actual"] == "valid"
    assert by_port["o"]["wildcard"] is False


def test_instance_filter_excludes_other_instances(graph) -> None:
    (unit,) = analysis.port_map(graph, "adder", instance="no_such_instance")
    assert unit["instances"] == []


def test_unknown_unit_returns_empty(graph) -> None:
    assert analysis.port_map(graph, "no_such_module") == []
