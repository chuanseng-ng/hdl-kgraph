"""Library helpers extracted from the CLI/MCP presentation layer (#70).

These graph traversals used to be inlined in command handlers; they now live in
``graph.analysis`` so the CLI and the MCP server share one implementation.
"""

from pathlib import Path

from hdl_kgraph.graph import analysis
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser


def _graph():
    p = SystemVerilogParser()
    irs = [
        p.parse(Path("leaf.sv"), "module leaf(input logic a, output logic y);\nendmodule\n"),
        p.parse(
            Path("top.sv"),
            "module top;\n  leaf u0();\n  leaf u1();\nendmodule\n",
        ),
    ]
    return build_graph(irs)


def test_resolve_unit_matches_by_name_excluding_stubs():
    g = _graph()
    leaf = analysis.resolve_unit(g, "leaf")
    assert leaf == ["leaf.sv::module:leaf"]
    assert analysis.resolve_unit(g, "nonexistent") == []


def test_instantiation_count_counts_incoming_instantiates():
    g = _graph()
    (leaf_id,) = analysis.resolve_unit(g, "leaf")
    assert analysis.instantiation_count(g, leaf_id) == 2  # u0, u1
    (top_id,) = analysis.resolve_unit(g, "top")
    assert analysis.instantiation_count(g, top_id) == 0  # a top, never instantiated


def test_kind_histograms_cover_every_node_and_edge():
    g = _graph()
    nodes = analysis.node_kind_histogram(g)
    edges = analysis.edge_kind_histogram(g)
    assert nodes["module"] == 2
    assert sum(nodes.values()) == g.number_of_nodes()
    assert sum(edges.values()) == g.number_of_edges()
    assert edges["instantiates"] == 2
