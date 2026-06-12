"""Pass-2 linker tests: confidence tiers, stubs, and binding resolution."""

from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.graph.analysis import find_top_modules, hierarchy_tree, instances_of
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.ids import file_node_id, stub_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import Edge, EdgeKind, Node, NodeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path) -> nx.MultiDiGraph:
    parser = SystemVerilogParser()
    irs: list[FileIR] = []
    for path in sorted(fixtures_dir.iterdir()):
        if path.suffix in parser.suffixes:
            irs.append(parser.parse(Path(path.name), path.read_text()))
    return build_graph(irs)


def edges_between(g: nx.MultiDiGraph, src: str, dst: str, kind: EdgeKind) -> list[dict]:
    if not g.has_edge(src, dst):
        return []
    return [d for d in g[src][dst].values() if d["kind"] is kind]


def test_unique_cross_file_instantiation_is_0_8(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "top.v::instance:top.u_counter",
        "simple_counter.sv::module:simple_counter",
        EdgeKind.INSTANTIATES,
    )
    assert edge["confidence"] == 0.8


def test_same_file_instantiation_is_1_0(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "wildcard_conn.sv::instance:wildcard_conn.u_leaf",
        "wildcard_conn.sv::module:wildcard_leaf",
        EdgeKind.INSTANTIATES,
    )
    assert edge["confidence"] == 1.0


def test_ambiguous_instantiation_emits_an_edge_per_candidate(graph: nx.MultiDiGraph) -> None:
    src = "uses_dup.sv::instance:uses_dup.u_leaf"
    edges = [
        (v, d) for _, v, d in graph.out_edges(src, data=True) if d["kind"] is EdgeKind.INSTANTIATES
    ]
    assert {v for v, _ in edges} == {
        "dup_leaf_a.sv::module:dup_leaf",
        "dup_leaf_b.sv::module:dup_leaf",
    }
    assert all(d["confidence"] == 0.6 for _, d in edges)


def test_unresolved_instantiation_creates_shared_stub(graph: nx.MultiDiGraph) -> None:
    stub_id = stub_node_id(NodeKind.MODULE, "ghost_mod")
    assert graph.nodes[stub_id]["attrs"]["unresolved"] is True
    (edge,) = edges_between(
        graph, "missing_child.sv::instance:missing_child.u_ghost", stub_id, EdgeKind.INSTANTIATES
    )
    # The reference is syntactically certain; the stub carries the uncertainty.
    assert edge["confidence"] == 1.0


def test_named_connection_resolves_to_port(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "top.v::instance:top.u_counter",
        "simple_counter.sv::port:simple_counter.count",
        EdgeKind.CONNECTS,
    )
    assert edge["attrs"]["expr_text"] == "value"
    assert edge["confidence"] == 0.8


def test_positional_connections_resolve_by_index(graph: nx.MultiDiGraph) -> None:
    src = "top_positional.v::instance:top_positional.u_adder"
    (edge,) = edges_between(graph, src, "adder.v::port:adder.cout", EdgeKind.CONNECTS)
    assert edge["attrs"]["position"] == 4


def test_positional_parameter_override_resolves_by_index(graph: nx.MultiDiGraph) -> None:
    src = "top_positional.v::instance:top_positional.u_adder"
    (edge,) = edges_between(graph, src, "adder.v::parameter:adder.WIDTH", EdgeKind.PARAMETERIZES)
    assert edge["attrs"]["value_text"] == "8"


def test_named_parameter_override_resolves(graph: nx.MultiDiGraph) -> None:
    (edge,) = edges_between(
        graph,
        "top.v::instance:top.u_counter",
        "simple_counter.sv::parameter:simple_counter.WIDTH",
        EdgeKind.PARAMETERIZES,
    )
    assert edge["attrs"]["value_text"] == "16"


def test_wildcard_connects_every_port(graph: nx.MultiDiGraph) -> None:
    src = "wildcard_conn.sv::instance:wildcard_conn.u_leaf"
    dsts = {v for _, v, d in graph.out_edges(src, data=True) if d["kind"] is EdgeKind.CONNECTS}
    assert dsts == {f"wildcard_conn.sv::port:wildcard_leaf.{p}" for p in ("clk", "rst_n", "ready")}


def test_connection_to_unresolved_target_creates_stub_ports(graph: nx.MultiDiGraph) -> None:
    src = "missing_child.sv::instance:missing_child.u_ghost"
    stub_port = stub_node_id(NodeKind.PORT, "ghost_mod.clk")
    (edge,) = edges_between(graph, src, stub_port, EdgeKind.CONNECTS)
    assert graph.nodes[stub_port]["attrs"]["unresolved"] is True
    # The stub module DECLARES its stub ports so the graph stays connected.
    (declares,) = edges_between(
        graph, stub_node_id(NodeKind.MODULE, "ghost_mod"), stub_port, EdgeKind.DECLARES
    )


def test_import_resolves_to_package(graph: nx.MultiDiGraph) -> None:
    edges = edges_between(
        graph, "imports_pkg.sv::module:imports_pkg", "my_pkg.sv::package:my_pkg", EdgeKind.IMPORTS
    )
    assert {e["attrs"]["symbol"] for e in edges} == {"*", "word_t"}
    assert all(e["confidence"] == 0.8 for e in edges)


def test_extends_same_file_and_external_stub(graph: nx.MultiDiGraph) -> None:
    (same_file,) = edges_between(
        graph, "classes.sv::class:burst_item", "classes.sv::class:base_item", EdgeKind.EXTENDS
    )
    assert same_file["confidence"] == 1.0
    stub = stub_node_id(NodeKind.CLASS, "uvm_test")
    (external,) = edges_between(graph, "ext_uvm.sv::class:smoke_test", stub, EdgeKind.EXTENDS)
    assert graph.nodes[stub]["attrs"]["unresolved"] is True


def test_find_top_modules_excludes_instantiated_and_stubs(graph: nx.MultiDiGraph) -> None:
    tops = {graph.nodes[t]["name"] for t in find_top_modules(graph)}
    assert "top" in tops
    assert "uses_dup" in tops
    assert "simple_counter" not in tops  # instantiated by top
    assert "ghost_mod" not in tops  # stub


def test_hierarchy_tree(graph: nx.MultiDiGraph) -> None:
    tree = hierarchy_tree(graph, "top.v::module:top")
    assert tree.module_name == "top"
    (child,) = tree.children
    assert child.instance_name == "u_counter"
    assert child.module_name == "simple_counter"
    assert child.confidence == 0.8
    assert child.children == []


def test_hierarchy_tree_marks_unresolved(graph: nx.MultiDiGraph) -> None:
    tree = hierarchy_tree(graph, "missing_child.sv::module:missing_child")
    (child,) = tree.children
    assert child.unresolved is True


def test_instances_of(graph: nx.MultiDiGraph) -> None:
    (rec,) = instances_of(graph, "simple_counter")
    assert rec["instance_name"] == "u_counter"
    assert rec["file"] == "top.v"
    assert rec["confidence"] == 0.8


# -- M2: shared-header dedupe and ref-site confidence ---------------------------


def test_duplicate_node_ids_ingested_once() -> None:
    """The same header spliced into two units must not fabricate ambiguity."""
    parser = SystemVerilogParser()
    leaf = "module leaf;\nendmodule\n"
    # Both units carry identical header-attributed nodes (same ids).
    ir_a = parser.parse(Path("hdr.svh"), leaf)
    ir_b = parser.parse(Path("hdr.svh"), leaf)
    ir_top = parser.parse(Path("top.sv"), "module top;\nleaf u_l ();\nendmodule\n")
    g = build_graph([ir_a, ir_b, ir_top])

    assert sum(1 for _, d in g.nodes(data=True) if d["name"] == "leaf") == 1
    (edge,) = edges_between(
        g, "top.sv::instance:top.u_l", "hdr.svh::module:leaf", EdgeKind.INSTANTIATES
    )
    assert edge["confidence"] == 0.8  # unique match, not 0.6 ambiguous


def test_duplicate_local_edges_ingested_once() -> None:
    parser = SystemVerilogParser()
    leaf = "module leaf;\nendmodule\n"
    ir_a = parser.parse(Path("hdr.svh"), leaf)
    ir_b = parser.parse(Path("hdr.svh"), leaf)
    g = build_graph([ir_a, ir_b])
    declares = [
        (u, v)
        for u, v, d in g.edges(data=True)
        if d["kind"] is EdgeKind.DECLARES and v == "hdr.svh::module:leaf"
    ]
    assert len(declares) == 1


def test_ref_confidence_caps_resolution() -> None:
    parser = SystemVerilogParser()
    ir_leaf = parser.parse(Path("leaf.sv"), "module leaf;\nendmodule\n")
    ir_top = parser.parse(Path("top.sv"), "module top;\nleaf u_l ();\nendmodule\n")
    for ref in ir_top.unresolved_refs:
        ref.confidence = 0.6  # as if the instantiation sat in an ifdef branch
    g = build_graph([ir_leaf, ir_top])
    (edge,) = edges_between(
        g, "top.sv::instance:top.u_l", "leaf.sv::module:leaf", EdgeKind.INSTANTIATES
    )
    assert edge["confidence"] == 0.6  # min(0.8 unique match, 0.6 ref site)


def test_duplicate_refs_emit_one_edge() -> None:
    """An unguarded header spliced into two units duplicates its refs across
    IRs; the resolved edge must still be emitted once."""
    parser = SystemVerilogParser()
    hdr = "module wrapper;\nleaf u_l ();\nendmodule\n"
    ir_a = parser.parse(Path("hdr.svh"), hdr)
    ir_b = parser.parse(Path("hdr.svh"), hdr)
    ir_leaf = parser.parse(Path("leaf.sv"), "module leaf;\nendmodule\n")
    g = build_graph([ir_a, ir_b, ir_leaf])
    (edge,) = edges_between(
        g, "hdr.svh::instance:wrapper.u_l", "leaf.sv::module:leaf", EdgeKind.INSTANTIATES
    )
    assert edge["confidence"] == 0.8


def test_dangling_edge_endpoints_materialize_as_typed_stubs() -> None:
    """An edge endpoint no parser emitted as a node must become an unresolved
    stub (with kind recovered from the id), never the attribute-less node
    networkx auto-creates — every analysis assumes ``data["kind"]`` exists."""
    ir = FileIR(path="m.sv")
    ir.nodes.append(
        Node(id="m.sv::module:m", kind=NodeKind.MODULE, name="m", qualified_name="m", file="m.sv")
    )
    ir.local_edges.append(
        Edge(src=file_node_id("missing.svh"), dst="m.sv::module:m", kind=EdgeKind.DECLARES)
    )
    ir.local_edges.append(Edge(src="m.sv::module:m", dst="???", kind=EdgeKind.DRIVES))
    warnings: list[str] = []
    g = build_graph([ir], warnings=warnings)

    header = g.nodes[file_node_id("missing.svh")]
    assert header["kind"] is NodeKind.FILE
    assert header["attrs"]["unresolved"] is True
    assert header["attrs"]["dangling"] is True

    unknown = g.nodes["???"]
    assert unknown["kind"] is NodeKind.SIGNAL  # unparseable id: fallback kind
    assert unknown["attrs"]["kind_unknown"] is True

    assert len(warnings) == 2
    assert all("dangling edge endpoint" in w for w in warnings)
    assert all("kind" in data for _, data in g.nodes(data=True))


def test_cross_ir_forward_reference_is_not_dangling() -> None:
    """A local edge may point at a node another IR contributes (e.g. a nested
    filelist's node emitted later in the list); that is not a dangling ref."""
    ir_edge = FileIR(path="a.f")
    ir_edge.nodes.append(
        Node(id="filelist:a.f", kind=NodeKind.FILELIST, name="a.f", qualified_name="a.f")
    )
    ir_edge.local_edges.append(Edge(src="filelist:a.f", dst="filelist:b.f", kind=EdgeKind.INCLUDES))
    ir_node = FileIR(path="b.f")
    ir_node.nodes.append(
        Node(id="filelist:b.f", kind=NodeKind.FILELIST, name="b.f", qualified_name="b.f")
    )
    warnings: list[str] = []
    g = build_graph([ir_edge, ir_node], warnings=warnings)
    assert warnings == []
    assert g.nodes["filelist:b.f"]["kind"] is NodeKind.FILELIST
