"""Unit tests for the SV preprocessor: directives, expansion, line mapping."""

from pathlib import Path

from hdl_kgraph.ids import file_node_id, stub_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.preprocessor import (
    LineOrigin,
    MacroTable,
    PreprocEmitter,
    PreprocessedFile,
    Preprocessor,
)
from hdl_kgraph.schema import CONFIDENCE_AMBIGUOUS, EdgeKind, NodeKind


def preprocess(
    tmp_path: Path, text: str, *, defines: dict[str, str | None] | None = None, **kw: object
) -> PreprocessedFile:
    path = tmp_path / "unit.sv"
    path.write_text(text)
    branch_mode = "select" if defines else "both"
    pre = Preprocessor(
        base=tmp_path,
        macros=MacroTable(defines),
        branch_mode=branch_mode,
        **kw,  # type: ignore[arg-type]
    )
    return pre.preprocess(path)


def out_lines(pp: PreprocessedFile) -> list[str]:
    return pp.text.splitlines()


# -- defines and expansion ------------------------------------------------------


def test_object_macro_expansion(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define WIDTH 8\nwire [`WIDTH-1:0] w;\n")
    assert out_lines(pp) == ["", "wire [8-1:0] w;"]
    assert pp.macro_defs[0].name == "WIDTH"
    assert pp.macro_uses[0].name == "WIDTH"
    assert pp.macro_uses[0].macro is pp.macro_defs[0]


def test_cli_define_table(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "wire [`WIDTH-1:0] w;\n", defines={"WIDTH": "16"})
    assert out_lines(pp) == ["wire [16-1:0] w;"]


def test_function_macro_with_defaults(tmp_path: Path) -> None:
    pp = preprocess(
        tmp_path,
        "`define ADD(a, b = 1) (a + b)\nassign x = `ADD(2);\nassign y = `ADD(2, 3);\n",
    )
    assert out_lines(pp) == ["", "assign x = (2 + 1);", "assign y = (2 + 3);"]


def test_function_macro_arg_colliding_with_later_param(tmp_path: Path) -> None:
    # An argument value that names a later parameter must not be re-substituted.
    pp = preprocess(tmp_path, "`define M(a, b) a + b\nwire x = `M(b, 2);\n")
    assert out_lines(pp)[1] == "wire x = b + 2;"


def test_function_macro_swapped_args(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define M(a, b) a + b\nwire x = `M(b, a);\n")
    assert out_lines(pp)[1] == "wire x = b + a;"


def test_function_macro_arg_with_backslash(tmp_path: Path) -> None:
    # Backslashes in argument values (e.g. escaped identifiers) pass through
    # literally; \1 must not be read as a regex group reference.
    pp = preprocess(tmp_path, "`define ID(x) x\nwire `ID(\\foo$bar );\nwire `ID(\\1 );\n")
    assert out_lines(pp)[1] == "wire \\foo$bar;"
    assert out_lines(pp)[2] == "wire \\1;"


def test_function_macro_no_params(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define F() 1\nx = `F();\n")
    assert out_lines(pp)[1] == "x = 1;"


def test_nested_expansion_and_recursion_guard(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define A `B\n`define B `A\nx = `A;\n")
    # Self-recursion stops; the inner use is left verbatim for tree-sitter.
    assert out_lines(pp)[2] == "x = `A;"


def test_undef(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define X 1\n`undef X\ny = `X;\n")
    assert out_lines(pp)[2] == "y = `X;"
    assert pp.macro_uses[-1].macro is None


def test_multiline_body_maps_to_invocation_line(tmp_path: Path) -> None:
    pp = preprocess(
        tmp_path,
        "`define MK(name) wire name``_a; \\\nwire name``_b;\nmodule m;\n`MK(x)\nendmodule\n",
    )
    lines = out_lines(pp)
    assert lines[0] == ""  # `define line
    assert lines[1] == ""  # continuation line
    assert lines[3] == "wire x_a; "
    assert lines[4] == "wire x_b;"
    # Both expanded lines map back to the invocation line (4).
    assert pp.line_map[3] == LineOrigin("unit.sv", 4)
    assert pp.line_map[4] == LineOrigin("unit.sv", 4)
    assert pp.line_map[5] == LineOrigin("unit.sv", 5)  # endmodule unaffected


def test_file_and_line_builtins(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "x = `__LINE__;\ny = `__FILE__;\n")
    assert out_lines(pp) == ["x = 1;", 'y = "unit.sv";']


def test_stringification_best_effort(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, '`define MSG(t) $display(`"t`")\n`MSG(hi);\n')
    assert out_lines(pp)[1] == '$display("hi");'


def test_unterminated_args_left_alone(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define F(a) a\nx = `F(1,\n")
    assert out_lines(pp)[1] == "x = `F(1,"
    assert any("invocation line" in w for w in pp.warnings)


def test_standard_directives_pass_through(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`timescale 1ns/1ps\n`default_nettype none\n")
    assert out_lines(pp) == ["`timescale 1ns/1ps", "`default_nettype none"]
    assert pp.macro_uses == []  # not recorded as unresolved uses


# -- conditionals ----------------------------------------------------------------


def test_ifdef_select_mode(tmp_path: Path) -> None:
    text = "`ifdef A\na;\n`elsif B\nb;\n`else\nc;\n`endif\n"
    assert out_lines(preprocess(tmp_path, text, defines={"A": None})) == [
        "",
        "a;",
        "",
        "",
        "",
        "",
        "",
    ]
    assert out_lines(preprocess(tmp_path, text, defines={"B": None})) == [
        "",
        "",
        "",
        "b;",
        "",
        "",
        "",
    ]
    assert out_lines(preprocess(tmp_path, text, defines={"X": None})) == [
        "",
        "",
        "",
        "",
        "",
        "c;",
        "",
    ]


def test_ifndef_and_nesting(tmp_path: Path) -> None:
    pp = preprocess(
        tmp_path,
        "`ifndef A\n`ifdef B\nx;\n`endif\ny;\n`endif\n",
        defines={"B": None},
    )
    assert out_lines(pp) == ["", "", "x;", "", "y;", ""]


def test_in_source_define_selects(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define A\n`ifdef A\nx;\n`else\ny;\n`endif\n")
    assert out_lines(pp) == ["", "", "x;", "", "", ""]
    assert all(not origin.ambiguous for origin in pp.line_map)


def test_both_branches_mode(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`ifdef A\na;\n`else\nb;\n`endif\n")
    # Both sides emitted; the normally-selected else side keeps confidence.
    assert out_lines(pp) == ["", "a;", "", "b;", ""]
    assert pp.line_map[1].ambiguous is True
    assert pp.line_map[3].ambiguous is False


def test_both_branches_ifndef_guard_not_ambiguous(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`ifndef GUARD\n`define GUARD\nbody;\n`endif\n")
    assert out_lines(pp) == ["", "", "body;", ""]
    assert pp.line_map[2].ambiguous is False


def test_select_mode_has_no_ambiguity(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`ifdef A\na;\n`else\nb;\n`endif\n", defines={"X": None})
    assert out_lines(pp) == ["", "", "", "b;", ""]


def test_unbalanced_conditionals_warn(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`endif\n`ifdef A\n", defines={"A": None})
    assert any("without matching" in w for w in pp.warnings)
    assert any("unterminated" in w for w in pp.warnings)


# -- includes --------------------------------------------------------------------


def test_include_splice_and_line_map(tmp_path: Path) -> None:
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "defs.svh").write_text("`define WIDTH 4\nwire [`WIDTH:0] inner;\n")
    (tmp_path / "unit.sv").write_text('before;\n`include "defs.svh"\nwire [`WIDTH:0] outer;\n')
    pre = Preprocessor(base=tmp_path, incdirs=[tmp_path / "inc"], branch_mode="both")
    pp = pre.preprocess(tmp_path / "unit.sv")
    assert out_lines(pp) == ["before;", "", "", "wire [4:0] inner;", "wire [4:0] outer;"]
    # Line map: before/directive lines from the unit, spliced lines from the
    # header, lines after the include back in the unit.
    assert pp.line_map[0] == LineOrigin("unit.sv", 1)
    assert pp.line_map[1] == LineOrigin("unit.sv", 2)  # the `include line itself
    assert pp.line_map[2] == LineOrigin("inc/defs.svh", 1)
    assert pp.line_map[3] == LineOrigin("inc/defs.svh", 2)
    assert pp.line_map[4] == LineOrigin("unit.sv", 3)
    assert pp.included_relpaths == {"inc/defs.svh"}
    assert pp.includes[0].resolved == "inc/defs.svh"
    # The macro defined in the header attributes to the header.
    assert pp.macro_defs[0].file == "inc/defs.svh"


def test_include_relative_to_includer(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "local.svh").write_text("local_line;\n")
    (tmp_path / "rtl" / "unit.sv").write_text('`include "local.svh"\n')
    pp = Preprocessor(base=tmp_path).preprocess(tmp_path / "rtl" / "unit.sv")
    assert out_lines(pp) == ["", "local_line;"]


def test_include_unresolved(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, '`include "nope.svh"\nafter;\n')
    assert out_lines(pp) == ["", "after;"]
    assert pp.includes[0].resolved is None
    assert any("cannot resolve" in w for w in pp.warnings)


def test_include_cycle(tmp_path: Path) -> None:
    (tmp_path / "a.svh").write_text('`include "b.svh"\na;\n')
    (tmp_path / "b.svh").write_text('`include "a.svh"\nb;\n')
    (tmp_path / "unit.sv").write_text('`include "a.svh"\n')
    pp = Preprocessor(base=tmp_path, branch_mode="both").preprocess(tmp_path / "unit.sv")
    assert any("cycle" in w for w in pp.warnings)
    assert "a;" in out_lines(pp) and "b;" in out_lines(pp)
    # The cycled edge still records its resolved target.
    assert all(ev.resolved is not None for ev in pp.includes)


def test_include_outside_build_root_skipped(tmp_path: Path) -> None:
    # A `..` include that resolves to a real file outside the build root must be
    # dropped, not spliced — otherwise it discloses out-of-tree source (#68).
    root = tmp_path / "proj"
    root.mkdir()
    (tmp_path / "secret.svh").write_text("`define SECRET_WIRE wire leaked;\n")
    unit = root / "unit.sv"
    unit.write_text('`include "../secret.svh"\n`SECRET_WIRE\n')
    pp = Preprocessor(base=root).preprocess(unit)
    assert any("escapes the build root" in w for w in pp.warnings)
    assert "wire leaked;" not in pp.text  # the out-of-tree header was not spliced
    assert not pp.included_relpaths


def test_include_via_trusted_incdir_outside_root_allowed(tmp_path: Path) -> None:
    # An incdir the operator explicitly configured is a trusted allowlist entry,
    # so a header found there is spliced even though it lives outside the root.
    root = tmp_path / "proj"
    root.mkdir()
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "defs.svh").write_text("`define VW wire vok;\n")
    unit = root / "unit.sv"
    unit.write_text('`include "defs.svh"\n`VW\n')
    pp = Preprocessor(base=root, incdirs=[vendor]).preprocess(unit)
    assert not any("escapes" in w for w in pp.warnings)
    assert "wire vok;" in pp.text


def test_macro_table_carries_across_files(tmp_path: Path) -> None:
    table = MacroTable()
    pre = Preprocessor(base=tmp_path, macros=table, branch_mode="both")
    (tmp_path / "first.sv").write_text("`define SHARED 7\n")
    (tmp_path / "second.sv").write_text("x = `SHARED;\n")
    pre.preprocess(tmp_path / "first.sv")
    pp = pre.preprocess(tmp_path / "second.sv")
    assert out_lines(pp) == ["x = 7;"]


# -- graph emission ----------------------------------------------------------------


def test_emitter_nodes_and_edges(tmp_path: Path) -> None:
    (tmp_path / "defs.svh").write_text("`define WIDTH 4\n")
    (tmp_path / "unit.sv").write_text('`include "defs.svh"\nwire [`WIDTH:0] w;\nx = `NOPE;\n')
    pp = Preprocessor(base=tmp_path, branch_mode="both").preprocess(tmp_path / "unit.sv")
    ir = FileIR(path=pp.path)
    PreprocEmitter().emit(pp, ir)

    nodes = {n.id: n for n in ir.nodes}
    macro_id = "defs.svh::macro:WIDTH"
    assert nodes[macro_id].kind is NodeKind.MACRO
    assert nodes[macro_id].attrs["body"] == "4"
    assert nodes[stub_node_id(NodeKind.MACRO, "NOPE")].attrs["unresolved"] is True
    assert file_node_id("defs.svh") in nodes

    edges = {(e.src, e.dst, e.kind) for e in ir.local_edges}
    assert (file_node_id("defs.svh"), macro_id, EdgeKind.DEFINES_MACRO) in edges
    assert (file_node_id("unit.sv"), macro_id, EdgeKind.USES_MACRO) in edges
    assert (file_node_id("unit.sv"), file_node_id("defs.svh"), EdgeKind.INCLUDES) in edges


def test_emitter_deduplicates_shared_header(tmp_path: Path) -> None:
    (tmp_path / "defs.svh").write_text("`define WIDTH 4\n")
    for unit in ("u1.sv", "u2.sv"):
        (tmp_path / unit).write_text('`include "defs.svh"\n')
    table = MacroTable()
    pre = Preprocessor(base=tmp_path, macros=table, branch_mode="both")
    emitter = PreprocEmitter()
    irs = []
    for unit in ("u1.sv", "u2.sv"):
        pp = pre.preprocess(tmp_path / unit)
        ir = FileIR(path=pp.path)
        emitter.emit(pp, ir)
        irs.append(ir)
    all_node_ids = [n.id for ir in irs for n in ir.nodes]
    assert len(all_node_ids) == len(set(all_node_ids))  # no duplicates across units
    defines_edges = [e for ir in irs for e in ir.local_edges if e.kind is EdgeKind.DEFINES_MACRO]
    assert len(defines_edges) == 1


def test_emitter_ambiguous_use_confidence(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, "`define M 1\n`ifdef A\nx = `M;\n`endif\n")
    ir = FileIR(path=pp.path)
    PreprocEmitter().emit(pp, ir)
    use = next(e for e in ir.local_edges if e.kind is EdgeKind.USES_MACRO)
    assert use.confidence == CONFIDENCE_AMBIGUOUS


def test_emitter_unresolved_include(tmp_path: Path) -> None:
    pp = preprocess(tmp_path, '`include "nope.svh"\n')
    ir = FileIR(path=pp.path)
    PreprocEmitter().emit(pp, ir)
    stub = next(n for n in ir.nodes if n.kind is NodeKind.INCLUDE_FILE)
    assert stub.attrs["unresolved"] is True
    edge = next(e for e in ir.local_edges if e.kind is EdgeKind.INCLUDES)
    assert edge.dst == stub.id
