"""Pass-1 VHDL parser tests against the M3 fixture corpus."""

from pathlib import Path

import pytest

from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


@pytest.fixture(scope="module")
def parser() -> VhdlParser:
    return VhdlParser()


def parse(parser: VhdlParser, fixtures_dir: Path, name: str, library: str = "work") -> FileIR:
    path = fixtures_dir / name
    return parser.parse(Path("tests/fixtures") / name, path.read_text(), library=library)


def nodes_of(ir: FileIR, kind: NodeKind) -> dict[str, object]:
    return {n.qualified_name: n for n in ir.nodes if n.kind is kind}


def refs_of(ir: FileIR, kind: EdgeKind) -> list[UnresolvedRef]:
    return [r for r in ir.unresolved_refs if r.edge_kind is kind]


def test_entity_ports_and_generic(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "alu.vhd")
    assert ir.parse_error_count == 0
    entities = nodes_of(ir, NodeKind.ENTITY)
    assert list(entities) == ["alu"]
    assert entities["alu"].language is Language.VHDL
    assert entities["alu"].attrs["library"] == "work"
    ports = nodes_of(ir, NodeKind.PORT)
    assert [p.name for p in ports.values()] == ["a", "b", "op", "result"]
    assert ports["alu.result"].attrs["direction"] == "out"
    assert ports["alu.a"].attrs["direction"] == "in"
    assert [p.attrs["index"] for p in ports.values()] == [0, 1, 2, 3]
    params = nodes_of(ir, NodeKind.PARAMETER)
    # Names are lowercased; the original casing is preserved.
    assert params["alu.width"].attrs["is_generic"] is True
    assert params["alu.width"].attrs["original_name"] == "WIDTH"
    assert params["alu.width"].attrs["default"] == "8"
    assert params["alu.width"].attrs["index"] == 0


def test_architecture_implements_and_process(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "alu.vhd")
    archs = nodes_of(ir, NodeKind.ARCHITECTURE)
    assert archs["rtl"].attrs["of_entity"] == "alu"
    (impl,) = refs_of(ir, EdgeKind.IMPLEMENTS)
    assert impl.target_name == "alu"
    processes = nodes_of(ir, NodeKind.PROCESS)
    (proc,) = processes.values()
    assert proc.attrs["sensitivity"] == ["a", "b", "op"]


def test_use_clauses_attach_to_next_design_unit(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "alu.vhd")
    uses = refs_of(ir, EdgeKind.USES_PACKAGE)
    assert {(u.target_name, u.attrs["library"]) for u in uses} == {
        ("std_logic_1164", "ieee"),
        ("numeric_std", "ieee"),
    }
    entity_id = nodes_of(ir, NodeKind.ENTITY)["alu"].id
    assert all(u.src_id == entity_id for u in uses)
    assert all(u.attrs["symbol"] == "all" for u in uses)


def test_library_kwarg_resolves_work(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "util_pkg.vhd", library="mylib")
    # `use work.util_pkg.all` resolves "work" to the file's own library.
    use = next(r for r in refs_of(ir, EdgeKind.USES_PACKAGE) if r.target_name == "util_pkg")
    assert use.attrs["library"] == "mylib"
    assert nodes_of(ir, NodeKind.ENTITY)["pkg_user"].attrs["library"] == "mylib"


def test_package_and_body(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "util_pkg.vhd")
    assert ir.parse_error_count == 0
    assert list(nodes_of(ir, NodeKind.VHDL_PACKAGE)) == ["util_pkg"]
    bodies = nodes_of(ir, NodeKind.PACKAGE_BODY)
    assert bodies["util_pkg"].attrs["of_package"] == "util_pkg"
    functions = nodes_of(ir, NodeKind.FUNCTION)
    assert {f.name for f in functions.values()} == {"clog2"}


def test_instantiation_styles(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "vhdl_top.vhd")
    assert ir.parse_error_count == 0
    instances = nodes_of(ir, NodeKind.INSTANCE)
    assert instances["rtl.u_counter"].attrs["style"] == "component"
    assert instances["rtl.u_counter"].attrs["target"] == "simple_counter"
    assert instances["rtl.u_alu"].attrs["style"] == "entity"
    assert instances["rtl.u_alu"].attrs["library"] == "work"
    assert instances["rtl.u_alu"].attrs["architecture"] == "rtl"
    inst_refs = {r.attrs.get("style"): r for r in refs_of(ir, EdgeKind.INSTANTIATES)}
    assert inst_refs["entity"].target_name == "alu"
    connects = [r for r in refs_of(ir, EdgeKind.CONNECTS) if "u_counter" in r.src_id]
    assert {c.attrs["port_name"] for c in connects} == {"clk", "rst_n", "en", "count"}
    (param,) = [r for r in refs_of(ir, EdgeKind.PARAMETERIZES) if "u_counter" in r.src_id]
    assert param.attrs["param_name"] == "width"
    assert param.attrs["value_text"] == "8"


def test_component_declaration_is_not_a_node(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "vhdl_top.vhd")
    # The component's ports must not be attributed to the architecture.
    ports = nodes_of(ir, NodeKind.PORT)
    assert all(q.startswith("vhdl_top.") for q in ports)
    names = {n.name for n in ir.nodes}
    assert "simple_counter" not in names  # only the entity declares it


def test_configuration_binds_refs(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "cfg_override.vhd")
    assert ir.parse_error_count == 0
    configs = nodes_of(ir, NodeKind.CONFIGURATION)
    assert configs["cfg_top_special"].attrs["of_entity"] == "cfg_top"
    binds = {r.attrs["role"]: r for r in refs_of(ir, EdgeKind.BINDS)}
    assert binds["configures"].target_name == "cfg_top"
    binding = binds["binding"]
    assert binding.target_name == "leaf_special"
    assert binding.attrs["of_entity"] == "cfg_top"
    assert binding.attrs["block"] == "rtl"
    assert binding.attrs["component"] == "leaf_default"
    assert binding.attrs["instances"] == ["u_leaf"]
    assert binding.attrs["architecture"] == "rtl"


def test_signals(parser, fixtures_dir) -> None:
    text = """
architecture rtl of x is
  signal s1, s2 : std_logic;
begin
end architecture;
"""
    ir = VhdlParser().parse(Path("inline.vhd"), text)
    signals = nodes_of(ir, NodeKind.SIGNAL)
    assert {s.name for s in signals.values()} == {"s1", "s2"}
    assert all(s.attrs["type_text"] == "std_logic" for s in signals.values())


def test_broken_file_yields_partial_results(parser, fixtures_dir) -> None:
    ir = parse(parser, fixtures_dir, "broken.vhd")
    assert ir.parse_error_count > 0
    # The valid entity before the error still extracts.
    assert "broken_ok" in nodes_of(ir, NodeKind.ENTITY)
