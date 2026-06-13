"""Aggregation tests (viz-scalability Phase 3): the collapsed community view."""

from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.graph import metrics
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.graph.metrics import ModuleMetrics
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import NodeKind
from hdl_kgraph.viz.aggregate import aggregate, aggregate_full


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    irs = [
        sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        sv.parse(Path("two_clock_cdc.sv"), (fixtures_dir / "two_clock_cdc.sv").read_text()),
    ]
    return build_graph(irs)


def _comm_of(g: nx.MultiDiGraph) -> dict[str, str]:
    comm_of: dict[str, str] = {}
    for i, part in enumerate(metrics.communities(g)):
        for node_id in part:
            comm_of[node_id] = str(i)
    return comm_of


def test_one_supernode_per_community_covering_every_unit(graph) -> None:
    proj = metrics.module_projection(graph)
    comm_of = _comm_of(graph)
    ranked = metrics.module_metrics(graph).modules
    agg = aggregate(proj, comm_of, ranked)
    assert len(agg.supernodes) == len(metrics.communities(graph))
    # Every projection unit lands in exactly one supernode.
    assert sum(s["count"] for s in agg.supernodes) == proj.number_of_nodes()
    assert {s["id"] for s in agg.supernodes} == set(comm_of.values())


def test_superlink_weights_match_cross_community_projection_sums(graph) -> None:
    proj = metrics.module_projection(graph)
    comm_of = _comm_of(graph)
    ranked = metrics.module_metrics(graph).modules
    agg = aggregate(proj, comm_of, ranked)
    expected: dict[tuple[str, str], int] = {}
    for u, v, data in proj.edges(data=True):
        cu, cv = comm_of[u], comm_of[v]
        if cu != cv:
            expected[(cu, cv)] = expected.get((cu, cv), 0) + data["weight"]
    got = {(s["source"], s["target"]): s["weight"] for s in agg.superlinks}
    assert got == expected


def _rank(*pairs: tuple[str, float]) -> list[ModuleMetrics]:
    return [
        ModuleMetrics(
            node_id=nid,
            name=nid.upper(),
            kind=NodeKind.MODULE,
            file="",
            fan_in=0,
            fan_out=0,
            betweenness=b,
            is_articulation=False,
            unresolved=False,
        )
        for nid, b in pairs
    ]


def test_synthetic_superlink_sums_and_labels() -> None:
    # Two communities with two cross-community edges (a->c, b->c) that must sum.
    proj = nx.DiGraph()
    proj.add_edge("a", "b", weight=2)  # intra community 0 (hidden)
    proj.add_edge("a", "c", weight=3)  # 0 -> 1
    proj.add_edge("b", "c", weight=1)  # 0 -> 1
    proj.add_edge("c", "d", weight=5)  # intra community 1 (hidden)
    comm_of = {"a": "0", "b": "0", "c": "1", "d": "1"}
    # 'a' is the top hub of community 0, 'c' of community 1.
    ranked = _rank(("a", 0.9), ("c", 0.8), ("d", 0.2), ("b", 0.1))

    agg = aggregate(proj, comm_of, ranked)
    assert {s["id"]: s["count"] for s in agg.supernodes} == {"0": 2, "1": 2}
    assert {s["id"]: s["label"] for s in agg.supernodes} == {"0": "A", "1": "C"}
    assert agg.superlinks == [{"source": "0", "target": "1", "weight": 4}]


def test_unit_membership_maps_leaves_to_their_unit(graph) -> None:
    um = metrics.unit_membership(graph)
    units = set(metrics.module_projection(graph).nodes())
    assert um and all(owner in units for owner in um.values())  # owners are units
    assert not (set(um) & units)  # a unit is never its own leaf


def test_aggregate_full_has_two_levels(graph) -> None:
    comm_of = _comm_of(graph)
    ranked = metrics.module_metrics(graph).modules
    um = metrics.unit_membership(graph)
    agg = aggregate_full(comm_of, ranked, um)
    # Community level: one per community, counting its units.
    assert {s["id"] for s in agg.supernodes} == set(comm_of.values())
    assert sum(s["count"] for s in agg.supernodes) == len(comm_of)  # every unit counted once
    # Unit level: one per projection unit, community consistent with comm_of.
    assert {u["id"] for u in agg.unitnodes} == set(comm_of)
    assert all(u["community"] == comm_of[u["id"]] for u in agg.unitnodes)
    # Leaf counts total the leaf→unit mappings.
    assert sum(u["count"] for u in agg.unitnodes) == len(um)


def test_aggregate_full_synthetic_counts_and_labels() -> None:
    # community 0 = {a (hub), b}; community 1 = {c}. Leaves: 2 under a, 1 under c.
    comm_of = {"a": "0", "b": "0", "c": "1"}
    ranked = _rank(("a", 0.9), ("c", 0.7), ("b", 0.1))
    unit_of = {"a.sig1": "a", "a.sig2": "a", "c.sig": "c"}
    agg = aggregate_full(comm_of, ranked, unit_of)
    assert {s["id"]: (s["label"], s["count"]) for s in agg.supernodes} == {
        "0": ("A", 2),  # 2 units, labeled after the hub
        "1": ("C", 1),
    }
    assert {u["id"]: (u["community"], u["count"]) for u in agg.unitnodes} == {
        "a": ("0", 2),
        "b": ("0", 0),
        "c": ("1", 1),
    }


def test_empty_and_edgeless_graphs_degrade_cleanly() -> None:
    assert aggregate(nx.DiGraph(), {}, []) == aggregate(nx.DiGraph(), {}, [])
    empty = aggregate(nx.DiGraph(), {}, [])
    assert empty.supernodes == [] and empty.superlinks == []

    proj = nx.DiGraph()
    proj.add_nodes_from(["a", "b"])  # nodes, no edges
    comm_of = {"a": "0", "b": "1"}
    agg = aggregate(proj, comm_of, _rank(("a", 0.5), ("b", 0.4)))
    assert {s["id"] for s in agg.supernodes} == {"0", "1"}
    assert agg.superlinks == []
