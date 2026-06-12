from hdl_kgraph.ids import decl_node_id, file_node_id, parse_node_id, stub_node_id
from hdl_kgraph.schema import NodeKind


def test_file_node_id() -> None:
    assert file_node_id("rtl/fifo.sv") == "file:rtl/fifo.sv"


def test_decl_node_id_uses_kind_and_scope() -> None:
    assert decl_node_id("rtl/fifo.sv", NodeKind.MODULE, "fifo") == "rtl/fifo.sv::module:fifo"
    assert (
        decl_node_id("rtl/top.v", NodeKind.INSTANCE, "top.u_counter")
        == "rtl/top.v::instance:top.u_counter"
    )
    assert decl_node_id("rtl/fifo.sv", NodeKind.PORT, "fifo.clk") == "rtl/fifo.sv::port:fifo.clk"


def test_stub_node_id_is_file_independent() -> None:
    assert stub_node_id(NodeKind.MODULE, "ghost_mod") == "unresolved:module:ghost_mod"
    assert stub_node_id(NodeKind.PORT, "ghost_mod.clk") == "unresolved:port:ghost_mod.clk"


def test_ids_are_distinct_across_kinds() -> None:
    a = decl_node_id("f.sv", NodeKind.FUNCTION, "m.crc8")
    b = decl_node_id("f.sv", NodeKind.TASK, "m.crc8")
    assert a != b


def test_parse_node_id_inverts_the_id_helpers() -> None:
    assert parse_node_id("file:rtl/defs.svh") == (NodeKind.FILE, "defs.svh")
    assert parse_node_id("filelist:sim/top.f") == (NodeKind.FILELIST, "top.f")
    assert parse_node_id("library:work") == (NodeKind.LIBRARY, "work")
    assert parse_node_id("macro:WIDTH") == (NodeKind.MACRO, "WIDTH")
    assert parse_node_id("macro:WIDTH@12") == (NodeKind.MACRO, "WIDTH")
    assert parse_node_id("unresolved:module:ghost_mod") == (NodeKind.MODULE, "ghost_mod")
    assert parse_node_id("unresolved:port:ghost_mod.clk") == (NodeKind.PORT, "ghost_mod.clk")
    assert parse_node_id("rtl/fifo.sv::module:fifo") == (NodeKind.MODULE, "fifo")
    assert parse_node_id("rtl/top.v::instance:top.u_counter") == (NodeKind.INSTANCE, "u_counter")
    assert parse_node_id("rtl/fifo.sv::port:fifo.clk@7.9") == (NodeKind.PORT, "clk")


def test_parse_node_id_rejects_unknown_shapes() -> None:
    assert parse_node_id("just_a_name") is None
    assert parse_node_id("bogus:thing") is None
    assert parse_node_id("f.sv::not_a_kind:x") is None
    assert parse_node_id("unresolved:not_a_kind:x") is None
