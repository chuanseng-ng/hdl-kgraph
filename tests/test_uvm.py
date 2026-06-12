"""UVM topology tests (M5): role classification and TEST_COVERS."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import uvm
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    irs = [
        sv.parse(Path("uvm_tb.sv"), (fixtures_dir / "uvm_tb.sv").read_text()),
        sv.parse(Path("verif_constructs.sv"), (fixtures_dir / "verif_constructs.sv").read_text()),
    ]
    return build_graph(irs)


def test_roles_classified_via_extends_chains(graph) -> None:
    by_name = {c.name: c for c in uvm.uvm_topology(graph)}
    assert by_name["verif_driver"].role == "driver"
    assert by_name["verif_monitor"].role == "monitor"
    assert by_name["verif_agent"].role == "agent"
    assert by_name["verif_scoreboard"].role == "scoreboard"
    assert by_name["verif_env"].role == "env"
    assert by_name["verif_base_test"].role == "test"


def test_transitive_chain_reaches_uvm_base(graph) -> None:
    by_name = {c.name: c for c in uvm.uvm_topology(graph)}
    smoke = by_name["verif_smoke_test"]
    assert smoke.role == "test"
    assert smoke.base_chain == ["verif_base_test", "uvm_test"]


def test_non_uvm_classes_are_not_components(graph) -> None:
    names = {c.name for c in uvm.uvm_topology(graph)}
    assert "verif_item" not in names  # plain rand object, no uvm_* base


def test_tb_top_test_covers_the_dut(graph) -> None:
    covers = {(u, v): d for u, v, d in graph.edges(data=True) if d["kind"] is EdgeKind.TEST_COVERS}
    tb_edge = covers[("uvm_tb.sv::module:tb_verif_top", "verif_constructs.sv::module:verif_dut")]
    assert tb_edge["confidence"] == 0.4
    assert tb_edge["attrs"]["evidence"] == "name_pattern"


def test_uvm_tests_cover_the_dut_too(graph) -> None:
    test_edges = {
        u
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.TEST_COVERS and v.endswith("module:verif_dut")
    }
    assert "uvm_tb.sv::class:verif_base_test" in test_edges
    assert "uvm_tb.sv::class:verif_smoke_test" in test_edges
    # ...but non-test components do not.
    assert "uvm_tb.sv::class:verif_env" not in test_edges


def test_build_graph_survives_dangling_edge_endpoint() -> None:
    """Regression: a dangling edge src used to leave an attribute-less node in
    the graph, and derive_test_covers crashed on it with KeyError 'kind'."""
    from hdl_kgraph.parser.base import FileIR
    from hdl_kgraph.schema import Edge, Node, NodeKind

    ir = FileIR(path="tb.sv")
    ir.nodes.append(
        Node(
            id="tb.sv::module:tb_top",
            kind=NodeKind.MODULE,
            name="tb_top",
            qualified_name="tb_top",
            file="tb.sv",
        )
    )
    ir.local_edges.append(
        Edge(src="file:gone.svh", dst="tb.sv::module:tb_top", kind=EdgeKind.DECLARES)
    )
    g = build_graph([ir])  # crashed inside derive_test_covers before the fix
    assert uvm.derive_test_covers(g) == []
