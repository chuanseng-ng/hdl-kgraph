"""Pass-1 parser tests against the M1 fixture corpus."""

from pathlib import Path

import pytest

from hdl_kgraph.parser.base import MAX_PARSE_ERRORS, FileIR, UnresolvedRef
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


@pytest.fixture(scope="module")
def parser() -> SystemVerilogParser:
    return SystemVerilogParser()


def parse(parser: SystemVerilogParser, fixtures_dir: Path, name: str) -> FileIR:
    path = fixtures_dir / name
    return parser.parse(Path("tests/fixtures") / name, path.read_text())


def nodes_of(ir: FileIR, kind: NodeKind) -> dict[str, object]:
    return {n.qualified_name: n for n in ir.nodes if n.kind is kind}


def refs_of(ir: FileIR, kind: EdgeKind) -> list[UnresolvedRef]:
    return [r for r in ir.unresolved_refs if r.edge_kind is kind]


def test_module_ports_and_parameters(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "simple_counter.sv")
    assert ir.parse_error_count == 0
    modules = nodes_of(ir, NodeKind.MODULE)
    assert list(modules) == ["simple_counter"]
    assert modules["simple_counter"].language is Language.SYSTEMVERILOG
    ports = nodes_of(ir, NodeKind.PORT)
    assert [p.name for p in ports.values()] == ["clk", "rst_n", "en", "count"]
    assert ports["simple_counter.count"].attrs["direction"] == "output"
    assert [p.attrs["index"] for p in ports.values()] == [0, 1, 2, 3]
    params = nodes_of(ir, NodeKind.PARAMETER)
    assert params["simple_counter.WIDTH"].attrs == {
        "is_localparam": False,
        "default": "8",
        "index": 0,
    }


def test_nonansi_verilog_ports(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "adder.v")
    assert ir.parse_error_count == 0
    assert nodes_of(ir, NodeKind.MODULE)["adder"].language is Language.VERILOG
    ports = nodes_of(ir, NodeKind.PORT)
    assert [p.name for p in ports.values()] == ["a", "b", "cin", "sum", "cout"]
    assert ports["adder.cin"].attrs["direction"] == "input"
    assert ports["adder.cout"].attrs["direction"] == "output"


def test_named_connections_and_param_override(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "top.v")
    instances = nodes_of(ir, NodeKind.INSTANCE)
    assert instances["top.u_counter"].attrs["target"] == "simple_counter"
    (inst_ref,) = refs_of(ir, EdgeKind.INSTANTIATES)
    assert inst_ref.target_name == "simple_counter"
    connects = refs_of(ir, EdgeKind.CONNECTS)
    assert {c.attrs["port_name"] for c in connects} == {"clk", "rst_n", "en", "count"}
    (param_ref,) = refs_of(ir, EdgeKind.PARAMETERIZES)
    assert param_ref.attrs["param_name"] == "WIDTH"
    assert param_ref.attrs["value_text"] == "16"


def test_positional_connections_and_param(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "top_positional.v")
    connects = refs_of(ir, EdgeKind.CONNECTS)
    assert [c.attrs["position"] for c in connects] == [0, 1, 2, 3, 4]
    assert connects[0].attrs["expr_text"] == "x"
    (param_ref,) = refs_of(ir, EdgeKind.PARAMETERIZES)
    assert param_ref.attrs["position"] == 0
    assert param_ref.attrs["value_text"] == "8"


def test_wildcard_connection(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "wildcard_conn.sv")
    (conn,) = refs_of(ir, EdgeKind.CONNECTS)
    assert conn.attrs["wildcard"] is True


def test_interface_declaration(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "bus_if.sv")
    assert "bus_if" in nodes_of(ir, NodeKind.INTERFACE)
    assert "bus_if.DATA_W" in nodes_of(ir, NodeKind.PARAMETER)


def test_interface_instantiation(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "uses_interface.sv")
    targets = {r.target_name for r in refs_of(ir, EdgeKind.INSTANTIATES)}
    assert targets == {"bus_if", "bus_consumer"}


def test_program_block(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "prog_block.sv")
    assert "prog_block" in nodes_of(ir, NodeKind.PROGRAM)


def test_package_contents(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "my_pkg.sv")
    assert "my_pkg" in nodes_of(ir, NodeKind.PACKAGE)
    assert "my_pkg.word_t" in nodes_of(ir, NodeKind.TYPEDEF)
    assert "my_pkg.state_e" in nodes_of(ir, NodeKind.ENUM)
    members = nodes_of(ir, NodeKind.ENUM_MEMBER)
    assert set(members) == {"my_pkg.state_e.IDLE", "my_pkg.state_e.BUSY", "my_pkg.state_e.DONE"}
    assert "my_pkg.req_t" in nodes_of(ir, NodeKind.STRUCT)
    assert "my_pkg.crc8" in nodes_of(ir, NodeKind.FUNCTION)
    localparam = nodes_of(ir, NodeKind.PARAMETER)["my_pkg.CRC_INIT"]
    assert localparam.attrs["is_localparam"] is True


def test_package_imports(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "imports_pkg.sv")
    imports = refs_of(ir, EdgeKind.IMPORTS)
    assert {(r.target_name, r.attrs["symbol"]) for r in imports} == {
        ("my_pkg", "*"),
        ("my_pkg", "word_t"),
    }
    module_id = nodes_of(ir, NodeKind.MODULE)["imports_pkg"].id
    assert all(r.src_id == module_id for r in imports)


def test_functions_and_tasks(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "funcs_tasks.sv")
    assert "funcs_tasks.invert" in nodes_of(ir, NodeKind.FUNCTION)
    assert "funcs_tasks.pulse" in nodes_of(ir, NodeKind.TASK)


def test_class_declaration_and_extends(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "classes.sv")
    classes = nodes_of(ir, NodeKind.CLASS)
    assert set(classes) == {"base_item", "burst_item"}
    (ref,) = refs_of(ir, EdgeKind.EXTENDS)
    assert ref.src_id == classes["burst_item"].id
    assert ref.target_name == "base_item"
    assert ref.attrs["package"] is None


def test_extends_external_and_package_scoped(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "ext_uvm.sv")
    refs = {r.src_id.rsplit(":", 1)[-1]: r for r in refs_of(ir, EdgeKind.EXTENDS)}
    assert refs["smoke_test"].target_name == "uvm_test"
    assert refs["pkg_scoped"].target_name == "base_cfg"
    assert refs["pkg_scoped"].attrs["package"] == "my_pkg"


def test_error_tolerance_partial_results(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "broken.sv")
    assert ir.parse_error_count > 0
    modules = nodes_of(ir, NodeKind.MODULE)
    assert "survives" in modules
    assert "survives.ok" in nodes_of(ir, NodeKind.PORT)


def test_parse_errors_carry_location_and_snippet(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "broken.sv")
    assert ir.parse_errors
    assert len(ir.parse_errors) <= ir.parse_error_count
    assert any("broken.sv:6: syntax error near `" in e for e in ir.parse_errors)


def test_record_parse_error_caps_details() -> None:
    ir = FileIR(path="x.sv")
    for line in range(MAX_PARSE_ERRORS + 5):
        ir.record_parse_error(f"x.sv:{line}: boom")
    assert ir.parse_error_count == MAX_PARSE_ERRORS + 5
    assert len(ir.parse_errors) == MAX_PARSE_ERRORS


def test_declares_edges_cover_all_non_file_nodes(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "my_pkg.sv")
    declared = {e.dst for e in ir.local_edges if e.kind is EdgeKind.DECLARES}
    non_file = {n.id for n in ir.nodes if n.kind is not NodeKind.FILE}
    assert declared == non_file


# Acceptance criterion: >=90% of expected fixture constructs extracted.
EXPECTED_CONSTRUCTS: list[tuple[str, NodeKind, str]] = [
    ("simple_counter.sv", NodeKind.MODULE, "simple_counter"),
    ("simple_counter.sv", NodeKind.PARAMETER, "simple_counter.WIDTH"),
    ("simple_counter.sv", NodeKind.PORT, "simple_counter.count"),
    ("top.v", NodeKind.INSTANCE, "top.u_counter"),
    ("adder.v", NodeKind.MODULE, "adder"),
    ("adder.v", NodeKind.PORT, "adder.cout"),
    ("top_positional.v", NodeKind.INSTANCE, "top_positional.u_adder"),
    ("bus_if.sv", NodeKind.INTERFACE, "bus_if"),
    ("uses_interface.sv", NodeKind.INSTANCE, "uses_interface.u_bus"),
    ("my_pkg.sv", NodeKind.PACKAGE, "my_pkg"),
    ("my_pkg.sv", NodeKind.TYPEDEF, "my_pkg.word_t"),
    ("my_pkg.sv", NodeKind.ENUM, "my_pkg.state_e"),
    ("my_pkg.sv", NodeKind.STRUCT, "my_pkg.req_t"),
    ("my_pkg.sv", NodeKind.FUNCTION, "my_pkg.crc8"),
    ("imports_pkg.sv", NodeKind.MODULE, "imports_pkg"),
    ("classes.sv", NodeKind.CLASS, "base_item"),
    ("classes.sv", NodeKind.CLASS, "burst_item"),
    ("ext_uvm.sv", NodeKind.CLASS, "smoke_test"),
    ("funcs_tasks.sv", NodeKind.FUNCTION, "funcs_tasks.invert"),
    ("funcs_tasks.sv", NodeKind.TASK, "funcs_tasks.pulse"),
    ("prog_block.sv", NodeKind.PROGRAM, "prog_block"),
    ("missing_child.sv", NodeKind.INSTANCE, "missing_child.u_ghost"),
    ("dup_leaf_a.sv", NodeKind.MODULE, "dup_leaf"),
    ("uses_dup.sv", NodeKind.INSTANCE, "uses_dup.u_leaf"),
    ("wildcard_conn.sv", NodeKind.MODULE, "wildcard_leaf"),
    ("broken.sv", NodeKind.MODULE, "survives"),
]


def test_at_least_90_percent_of_constructs_extracted(parser, fixtures_dir) -> None:
    extracted = 0
    irs: dict[str, FileIR] = {}
    for fixture, kind, qualified in EXPECTED_CONSTRUCTS:
        if fixture not in irs:
            irs[fixture] = parse(parser, fixtures_dir, fixture)
        if qualified in nodes_of(irs[fixture], kind):
            extracted += 1
    ratio = extracted / len(EXPECTED_CONSTRUCTS)
    assert ratio >= 0.9, f"only {extracted}/{len(EXPECTED_CONSTRUCTS)} constructs extracted"
