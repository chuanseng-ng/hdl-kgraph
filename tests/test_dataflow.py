"""Dataflow tests (M5): SIGNAL nodes, DRIVES/READS, instance-port flow."""

from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import EdgeKind, NodeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    irs = [
        sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        vhdl.parse(Path("dataflow.vhd"), (fixtures_dir / "dataflow.vhd").read_text()),
        sv.parse(Path("classes.sv"), (fixtures_dir / "classes.sv").read_text()),
    ]
    return build_graph(irs)


def _edges(g, kind: EdgeKind):
    return [(u, v, d) for u, v, d in g.edges(data=True) if d["kind"] is kind]


def _edge(g, kind: EdgeKind, src: str, dst: str):
    for u, v, d in _edges(g, kind):
        if u == src and v == dst:
            return d
    return None


def test_signal_nodes_extracted_with_attrs(graph) -> None:
    stage = graph.nodes["dataflow.sv::signal:df_top.stage"]
    assert stage["kind"] is NodeKind.SIGNAL
    assert stage["attrs"]["is_net"] is False
    valid = graph.nodes["dataflow.sv::signal:df_top.valid"]
    assert valid["attrs"]["is_net"] is True
    assert valid["attrs"]["net_type"] == "wire"


def test_continuous_assign_drives_and_reads(graph) -> None:
    proc = "dataflow.sv::process:df_top.assign@29"
    assert graph.nodes[proc]["attrs"]["style"] == "continuous_assign"
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.sv::signal:df_top.valid")
    read = _edge(graph, EdgeKind.READS, proc, "dataflow.sv::signal:df_top.stage")
    assert read is not None and read["confidence"] == 1.0


def test_decl_initializer_is_a_drive(graph) -> None:
    proc = "dataflow.sv::process:df_top.assign@26"
    assert graph.nodes[proc]["attrs"]["decl_init"] is True
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.sv::signal:df_top.doubled")
    assert _edge(graph, EdgeKind.READS, proc, "dataflow.sv::signal:df_top.stage")


def test_always_ff_block_dataflow(graph) -> None:
    proc = "dataflow.sv::process:df_top.always@32"
    attrs = graph.nodes[proc]["attrs"]
    assert attrs["style"] == "always_ff"
    assert {e["name"] for e in attrs["sensitivity"]} == {"clk", "rst_n"}
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.sv::signal:df_top.stage")
    assert _edge(graph, EdgeKind.READS, proc, "dataflow.sv::port:df_top.din")


def test_undeclared_name_becomes_implicit_signal_stub(graph) -> None:
    proc = "dataflow.sv::process:df_top.assign@29"
    edge = _edge(graph, EdgeKind.READS, proc, "unresolved:signal:df_top.en_missing")
    assert edge is not None
    assert edge["confidence"] <= 0.6
    assert graph.nodes["unresolved:signal:df_top.en_missing"]["attrs"]["unresolved"]


def test_parameter_reads_are_dropped(graph) -> None:
    # WIDTH appears in expressions everywhere; constants are not dataflow.
    assert not any(
        v.endswith(":df_top.WIDTH") or v == "unresolved:signal:df_top.WIDTH"
        for _, v, d in graph.edges(data=True)
        if d["kind"] in (EdgeKind.READS, EdgeKind.DRIVES)
    )


def test_for_loop_variable_is_not_dataflow(graph) -> None:
    proc = "dataflow.sv::process:df_top.always@37"
    targets = {v for u, v, d in graph.edges(data=True) if u == proc}
    assert not any(t.endswith(".idx") for t in targets)
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.sv::signal:df_top.mem")


def test_memory_write_drives_root_and_reads_index(graph) -> None:
    proc = "dataflow.sv::process:df_top.always@43"
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.sv::signal:df_top.mem")
    assert _edge(graph, EdgeKind.READS, proc, "dataflow.sv::port:df_top.din")


def test_instance_ports_derive_dataflow(graph) -> None:
    inst = "dataflow.sv::instance:df_top.u_sub"
    reads = _edge(graph, EdgeKind.READS, inst, "dataflow.sv::signal:df_top.valid")
    assert reads is not None  # .i(valid): input port reads the actual
    assert reads["attrs"]["via_port"] == "i"
    drives = _edge(graph, EdgeKind.DRIVES, inst, "unresolved:signal:df_top.sub_out")
    assert drives is not None  # .o(sub_out): output drives the (implicit) actual


def test_class_properties_are_not_signals(graph) -> None:
    assert not any(
        d["kind"] is NodeKind.SIGNAL and "base_item" in n for n, d in graph.nodes(data=True)
    )


def test_vhdl_process_dataflow_resolves_entity_ports(graph) -> None:
    proc = "dataflow.vhd::process:rtl.reg_p"
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.vhd::signal:rtl.stage")
    assert _edge(graph, EdgeKind.READS, proc, "dataflow.vhd::port:df_reg.d")


def test_vhdl_concurrent_assignment_is_a_process(graph) -> None:
    proc = "dataflow.vhd::process:rtl.assign@18"
    assert graph.nodes[proc]["attrs"]["style"] == "concurrent_assignment"
    assert _edge(graph, EdgeKind.DRIVES, proc, "dataflow.vhd::port:df_reg.q")
    assert _edge(graph, EdgeKind.READS, proc, "dataflow.vhd::signal:rtl.stage")
