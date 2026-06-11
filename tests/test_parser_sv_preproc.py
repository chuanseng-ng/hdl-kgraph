"""SV parser + preprocessor integration: spans, file attribution, confidence."""

from pathlib import Path

from hdl_kgraph.ids import file_node_id
from hdl_kgraph.parser.preprocessor import MacroTable, PreprocessedFile, Preprocessor
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import CONFIDENCE_AMBIGUOUS, EdgeKind, NodeKind


def preprocess(tmp_path: Path, unit: str, **kw: object) -> PreprocessedFile:
    pre = Preprocessor(base=tmp_path, **kw)  # type: ignore[arg-type]
    return pre.preprocess(tmp_path / unit)


def parse(tmp_path: Path, unit: str, **kw: object):
    pp = preprocess(tmp_path, unit, **kw)
    return SystemVerilogParser().parse(Path(pp.path), pp.text, line_map=pp.line_map), pp


def test_header_declarations_attribute_to_header(tmp_path: Path) -> None:
    (tmp_path / "ports.svh").write_text("input logic clk,\ninput logic rst\n")
    (tmp_path / "m.sv").write_text('module m (\n`include "ports.svh"\n);\nendmodule\n')
    ir, _ = parse(tmp_path, "m.sv")

    module = next(n for n in ir.nodes if n.kind is NodeKind.MODULE)
    assert module.file == "m.sv"
    assert module.line_span == (1, 4)  # start and end both in m.sv

    clk = next(n for n in ir.nodes if n.kind is NodeKind.PORT and n.name == "clk")
    assert clk.file == "ports.svh"
    assert clk.id == "ports.svh::port:m.clk"
    assert clk.line_span == (1, 1)


def test_module_span_within_one_file(tmp_path: Path) -> None:
    (tmp_path / "m.sv").write_text("`define X\nmodule m;\nwire w;\nendmodule\n")
    ir, _ = parse(tmp_path, "m.sv")
    module = next(n for n in ir.nodes if n.kind is NodeKind.MODULE)
    assert module.file == "m.sv"
    assert module.line_span == (2, 4)


def test_file_scope_header_module_declared_by_header_file(tmp_path: Path) -> None:
    (tmp_path / "extra.svh").write_text("module from_header;\nendmodule\n")
    (tmp_path / "m.sv").write_text('`include "extra.svh"\nmodule m;\nendmodule\n')
    ir, _ = parse(tmp_path, "m.sv")

    hdr_mod = next(n for n in ir.nodes if n.name == "from_header")
    assert hdr_mod.file == "extra.svh"
    declares = next(e for e in ir.local_edges if e.dst == hdr_mod.id)
    assert declares.src == file_node_id("extra.svh")
    own_mod = next(n for n in ir.nodes if n.name == "m")
    declares_own = next(e for e in ir.local_edges if e.dst == own_mod.id)
    assert declares_own.src == file_node_id("m.sv")


def test_macro_instantiated_module_spans_invocation_line(tmp_path: Path) -> None:
    (tmp_path / "m.sv").write_text(
        "`define MK_FIFO(name) fifo name (.clk(clk), \\\n.rst(rst));\n"
        "module top;\n"
        "`MK_FIFO(u_rx)\n"
        "endmodule\n"
    )
    ir, _ = parse(tmp_path, "m.sv")
    inst = next(n for n in ir.nodes if n.kind is NodeKind.INSTANCE)
    assert inst.name == "u_rx"
    assert inst.file == "m.sv"
    assert inst.line_span == (4, 4)  # the `MK_FIFO invocation line
    ref = next(r for r in ir.unresolved_refs if r.edge_kind is EdgeKind.INSTANTIATES)
    assert ref.target_name == "fifo"
    assert ref.line_span == (4, 4)
    assert ref.confidence == 1.0


def test_conditional_nodes_marked_and_downgraded(tmp_path: Path) -> None:
    (tmp_path / "m.sv").write_text(
        "module top;\n"
        "`ifdef USE_FIFO\n"
        "fifo u_q ();\n"
        "`else\n"
        "stack u_q ();\n"
        "`endif\n"
        "endmodule\n"
    )
    ir, _ = parse(tmp_path, "m.sv", branch_mode="both")

    instances = [n for n in ir.nodes if n.kind is NodeKind.INSTANCE]
    assert len(instances) == 2
    fifo_inst = next(n for n in instances if n.attrs.get("target") == "fifo")
    stack_inst = next(n for n in instances if n.attrs.get("target") == "stack")
    # The non-selected ifdef side is conditional; the else side is what a
    # define-less compile would elaborate.
    assert fifo_inst.attrs.get("conditional") is True
    assert stack_inst.attrs.get("conditional") is None

    refs = {r.target_name: r for r in ir.unresolved_refs if r.edge_kind is EdgeKind.INSTANTIATES}
    assert refs["fifo"].confidence == CONFIDENCE_AMBIGUOUS
    assert refs["stack"].confidence == 1.0

    declares = {e.dst: e for e in ir.local_edges if e.kind is EdgeKind.DECLARES}
    assert declares[fifo_inst.id].confidence == CONFIDENCE_AMBIGUOUS
    assert declares[stack_inst.id].confidence == 1.0


def test_select_mode_drops_alternative(tmp_path: Path) -> None:
    (tmp_path / "m.sv").write_text(
        "module top;\n`ifdef USE_FIFO\nfifo u_q ();\n`else\nstack u_q ();\n`endif\nendmodule\n"
    )
    ir, _ = parse(tmp_path, "m.sv", macros=MacroTable({"USE_FIFO": None}))
    instances = [n for n in ir.nodes if n.kind is NodeKind.INSTANCE]
    assert [n.attrs.get("target") for n in instances] == ["fifo"]
    assert instances[0].attrs.get("conditional") is None


def test_parse_without_line_map_unchanged(tmp_path: Path) -> None:
    text = "module m;\nwire w;\nendmodule\n"
    ir = SystemVerilogParser().parse(Path("m.sv"), text)
    module = next(n for n in ir.nodes if n.kind is NodeKind.MODULE)
    assert module.line_span == (1, 3)
    assert module.file == "m.sv"
