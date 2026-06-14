"""M7 acceptance tests: native-frontend semantic enrichment.

The headline acceptance criterion: on a parameterized generate loop, the
enriched graph's instance count matches elaborated reality, while a plain build
(enrichment off) still works and produces the heuristic graph. pyslang is a
core dependency, so these run unconditionally — no ``importorskip`` guard.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.enrich import EnrichmentInput, available_backends, run_enrichment
from hdl_kgraph.enrich.base import Capabilities, EnrichmentResult
from hdl_kgraph.enrich.stub_backend import StubBackend
from hdl_kgraph.graph.builder import add_or_upgrade_edge, ensure_node
from hdl_kgraph.ids import elab_node_id
from hdl_kgraph.pipeline import run_build, run_update
from hdl_kgraph.schema import CONFIDENCE_UNIQUE_MATCH, Edge, EdgeKind, Node, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


@pytest.fixture
def project(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A project dir holding the parameterized-generate fixture."""
    shutil.copy(fixtures_dir / "param_generate.sv", tmp_path / "param_generate.sv")
    return tmp_path


def _db(root: Path) -> SqliteStore:
    return SqliteStore(root / ".hdl-kgraph" / "graph.db")


def _instances(graph: nx.MultiDiGraph) -> list[str]:
    return [n for n, d in graph.nodes(data=True) if d["kind"] is NodeKind.INSTANCE]


# -- acceptance: instance counts match elaboration ---------------------------


def test_enrich_unrolls_generate_loop(project: Path) -> None:
    report = run_build(project, options=BuildOptions(enrich=True))
    assert report.enriched
    assert report.enrich_backends == ["slang"]
    assert report.discrepancy_count == 1

    graph, _, _ = _db(project).load()
    # One syntactic instance + four elaborated iterations of u_leaf.
    elaborated = [
        n
        for n, d in graph.nodes(data=True)
        if d["kind"] is NodeKind.INSTANCE and d["attrs"].get("source") == "elaborated"
    ]
    assert len(elaborated) == 4
    assert elab_node_id(NodeKind.INSTANCE, "param_top.g_leaf[0].u_leaf") in graph

    # The syntactic instance is annotated with the true count.
    syntactic = graph.nodes["param_generate.sv::instance:param_top.u_leaf"]
    assert syntactic["attrs"]["elaborated_count"] == 4


def test_enrich_records_instance_count_discrepancy(project: Path) -> None:
    run_build(project, options=BuildOptions(enrich=True))
    items = _db(project).load_discrepancies()
    assert len(items) == 1
    d = items[0]
    assert d.kind == "instance_count"
    assert d.backend == "slang"
    assert d.heuristic == "1"
    assert d.elaborated == "4"


def test_enrich_upgrades_instantiates_edge(project: Path) -> None:
    run_build(project, options=BuildOptions(enrich=True))
    graph, _, _ = _db(project).load()
    inst = "param_generate.sv::instance:param_top.u_leaf"
    target = next(
        n for n, d in graph.nodes(data=True) if d["kind"] is NodeKind.MODULE and d["name"] == "leaf"
    )
    data = next(iter(graph[inst][target].values()))
    assert data["confidence"] == 1.0
    assert data["attrs"]["source"] == "elaborated"
    assert data["attrs"]["backend"] == "slang"


# -- acceptance: works with enrichment off -----------------------------------


def test_default_build_is_heuristic_only(project: Path) -> None:
    report = run_build(project)  # no enrich
    assert not report.enriched
    assert report.discrepancy_count == 0

    graph, _, _ = _db(project).load()
    assert len(_instances(graph)) == 1  # the single syntactic u_leaf
    assert not any(d["attrs"].get("source") == "elaborated" for _, d in graph.nodes(data=True))
    assert _db(project).load_discrepancies() == []


# -- idempotency & incremental safety ----------------------------------------


def test_enrichment_is_idempotent(project: Path) -> None:
    run_build(project, options=BuildOptions(enrich=True))
    first, _, _ = _db(project).load()
    run_update(project, options=BuildOptions(enrich=True), full=True)
    second, _, _ = _db(project).load()
    assert second.number_of_nodes() == first.number_of_nodes()
    assert second.number_of_edges() == first.number_of_edges()


def test_toggling_enrich_forces_full_rebuild(project: Path) -> None:
    run_build(project)  # heuristic build first
    report = run_update(project, options=BuildOptions(enrich=True))
    assert report.full_rebuild_reason is not None
    assert report.build is not None and report.build.enriched


def test_duplicate_instantiation_is_not_flagged_as_generate(tmp_path: Path) -> None:
    # `mid` is instantiated twice, each with exactly one `u_leaf`. The child's
    # multiplicity within a single `mid` is 1, so it must not be reported as a
    # generate/array expansion (no false instance_count discrepancy).
    (tmp_path / "dup.sv").write_text(
        "module leaf (input logic clk);\nendmodule\n"
        "module mid (input logic clk);\n  leaf u_leaf (.clk(clk));\nendmodule\n"
        "module dup_top (input logic clk);\n"
        "  mid u_mid0 (.clk(clk));\n  mid u_mid1 (.clk(clk));\nendmodule\n"
    )
    report = run_build(tmp_path, options=BuildOptions(enrich=True))
    assert report.discrepancy_count == 0
    assert _db(tmp_path).load_discrepancies() == []


# -- graceful degradation ----------------------------------------------------


def test_enrich_on_unelaboratable_design_keeps_heuristic_graph(tmp_path: Path) -> None:
    # `top` instantiates a module that does not exist: slang cannot fully
    # elaborate, but the build must still succeed on the heuristic graph.
    (tmp_path / "broken_top.sv").write_text(
        "module broken_top(input logic clk);\n  missing_child u_child(.clk(clk));\nendmodule\n"
    )
    report = run_build(tmp_path, options=BuildOptions(enrich=True))
    assert report.enriched
    graph, _, _ = _db(tmp_path).load()
    assert any(d["kind"] is NodeKind.INSTANCE for _, d in graph.nodes(data=True))


# -- merge-helper units ------------------------------------------------------


def test_add_or_upgrade_edge_upgrades_in_place() -> None:
    g = nx.MultiDiGraph()
    a = Node(id="a", kind=NodeKind.INSTANCE, name="a")
    b = Node(id="b", kind=NodeKind.MODULE, name="b")
    ensure_node(g, a)
    ensure_node(g, b)
    add_or_upgrade_edge(
        g, Edge("a", "b", EdgeKind.INSTANTIATES, confidence=CONFIDENCE_UNIQUE_MATCH)
    )
    upgraded = add_or_upgrade_edge(
        g, Edge("a", "b", EdgeKind.INSTANTIATES, confidence=1.0, attrs={"source": "elaborated"})
    )
    assert upgraded
    assert g.number_of_edges() == 1
    data = next(iter(g["a"]["b"].values()))
    assert data["confidence"] == 1.0
    assert data["attrs"]["source"] == "elaborated"


def test_ensure_node_preserves_existing() -> None:
    g = nx.MultiDiGraph()
    ensure_node(g, Node(id="x", kind=NodeKind.MODULE, name="orig"))
    ensure_node(g, Node(id="x", kind=NodeKind.MODULE, name="replacement"))
    assert g.nodes["x"]["name"] == "orig"


# -- backend / runner plumbing -----------------------------------------------


def test_stub_backend_is_a_noop() -> None:
    g = nx.MultiDiGraph()
    g.add_node("m", kind=NodeKind.MODULE, name="m", attrs={})
    result = StubBackend().enrich(EnrichmentInput(), g)
    assert isinstance(result, EnrichmentResult)
    assert result.new_nodes == [] and result.upgrades == []


def test_slang_backend_is_available_and_capable() -> None:
    backends = available_backends()
    assert [b.name for b in backends] == ["slang"]
    caps = backends[0].capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.unrolls_generates


def test_run_enrichment_applies_stub_without_change() -> None:
    g = nx.MultiDiGraph()
    g.add_node("m", kind=NodeKind.MODULE, name="m", attrs={})
    report = run_enrichment(g, EnrichmentInput(), [StubBackend()])
    assert report.backends == []  # stub has no matching files (empty input)
    assert g.number_of_nodes() == 1


# -- CLI surface -------------------------------------------------------------


def test_cli_build_enrich_and_discrepancies(project: Path) -> None:
    from click.testing import CliRunner

    from hdl_kgraph.cli.main import main

    runner = CliRunner()
    built = runner.invoke(main, ["build", str(project), "--enrich"])
    assert built.exit_code == 0, built.output
    assert "enriched via slang" in built.output

    db = ["--db", str(project / ".hdl-kgraph" / "graph.db")]
    listed = runner.invoke(main, ["discrepancies", *db])
    assert listed.exit_code == 0, listed.output
    assert "instance_count" in listed.output

    as_json = runner.invoke(main, ["discrepancies", *db, "--json"])
    assert as_json.exit_code == 0
    import json

    payload = json.loads(as_json.output)
    assert payload[0]["kind"] == "instance_count"
    assert payload[0]["elaborated"] == "4"


def test_reconcile_reports_wrong_target() -> None:
    """Elaboration binding an instance to a different module than the heuristic
    name match is recorded as a wrong_target discrepancy."""
    from hdl_kgraph.enrich.slang_backend import _reconcile

    g = nx.MultiDiGraph()
    ensure_node(g, Node(id="m", kind=NodeKind.MODULE, name="top"))
    ensure_node(g, Node(id="i", kind=NodeKind.INSTANCE, name="u_x"))
    ensure_node(g, Node(id="foo", kind=NodeKind.MODULE, name="foo"))
    g.add_edge("m", "i", kind=EdgeKind.DECLARES, confidence=1.0, attrs={})
    g.add_edge("i", "foo", kind=EdgeKind.INSTANTIATES, confidence=0.8, attrs={})

    result = EnrichmentResult()
    # Elaboration says top.u_x binds to "bar", not the heuristic guess "foo".
    _reconcile(g, {("top", "u_x"): {"bar": ["top.u_x"]}}, result)
    assert len(result.discrepancies) == 1
    assert result.discrepancies[0].kind == "wrong_target"
    assert result.discrepancies[0].heuristic == "foo"
    assert result.discrepancies[0].elaborated == "bar"


def test_cli_discrepancies_empty_without_enrich(project: Path) -> None:
    from click.testing import CliRunner

    from hdl_kgraph.cli.main import main

    runner = CliRunner()
    assert runner.invoke(main, ["build", str(project)]).exit_code == 0
    db = ["--db", str(project / ".hdl-kgraph" / "graph.db")]
    result = runner.invoke(main, ["discrepancies", *db])
    assert result.exit_code == 0
    assert "no discrepancies" in result.output
