"""M7 acceptance tests: the GHDL (VHDL) enrichment backend.

Unlike pyslang, GHDL is a system binary (``pyGHDL`` ships with it, not via pip),
so tests that actually analyse VHDL are gated behind ``@ghdl`` and skip cleanly
where GHDL is absent. The backend's pure-Python surface — registration,
capabilities, and the reconciliation/id-mapping logic — runs unconditionally:
that is the coverage that protects the merge on a machine without GHDL.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.enrich import available_backends
from hdl_kgraph.enrich.base import Capabilities, EnrichmentResult
from hdl_kgraph.enrich.ghdl_backend import (
    GhdlBackend,
    _generate_count,
    _reconcile,
    _walk_statements,
)
from hdl_kgraph.graph.builder import ensure_node
from hdl_kgraph.ids import elab_node_id
from hdl_kgraph.pipeline import run_build
from hdl_kgraph.schema import EdgeKind, Language, Node, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore

ghdl = pytest.mark.skipif(
    shutil.which("ghdl") is None or importlib.util.find_spec("pyGHDL.libghdl") is None,
    reason="ghdl binary and pyGHDL bindings required for the VHDL enrichment backend",
)


def _db(root: Path) -> SqliteStore:
    return SqliteStore(root / ".hdl-kgraph" / "graph.db")


# -- unconditional: backend plumbing (no GHDL needed) ------------------------


def test_ghdl_backend_availability_never_raises() -> None:
    # On a box without ghdl, available() returns False rather than raising, and
    # the registry simply omits it (still exposing slang).
    backend = GhdlBackend()
    assert backend.available() in (True, False)
    names = [b.name for b in available_backends()]
    if backend.available():
        assert "ghdl" in names
    else:
        assert names == ["slang"]


def test_ghdl_capabilities() -> None:
    caps = GhdlBackend().capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.resolves_params
    assert caps.unrolls_generates
    assert caps.resolves_types
    assert not caps.resolves_defparam  # VHDL has no defparam


# -- unconditional: reconciliation logic (the highest-value unit) ------------


def _vhdl_graph() -> nx.MultiDiGraph:
    """A minimal heuristic graph: cfg_top(rtl) instantiates leaf_default as u_leaf."""
    g = nx.MultiDiGraph()
    ensure_node(
        g, Node(id="arch", kind=NodeKind.ARCHITECTURE, name="rtl", attrs={"of_entity": "cfg_top"})
    )
    ensure_node(g, Node(id="inst", kind=NodeKind.INSTANCE, name="u_leaf", language=Language.VHDL))
    ensure_node(g, Node(id="leaf_default", kind=NodeKind.ENTITY, name="leaf_default"))
    g.add_edge("arch", "inst", kind=EdgeKind.DECLARES, confidence=1.0, attrs={})
    g.add_edge("inst", "leaf_default", kind=EdgeKind.INSTANTIATES, confidence=0.8, attrs={})
    return g


def test_reconcile_reports_wrong_target() -> None:
    # A configuration rebinds u_leaf to leaf_special; the heuristic guessed the
    # like-named leaf_default — recorded as a wrong_target discrepancy, no upgrade.
    g = _vhdl_graph()
    result = EnrichmentResult()
    _reconcile(g, {(("cfg_top", "rtl"), "u_leaf"): ("leaf_special", ["cfg_top.u_leaf"])}, result)

    assert result.upgrades == []
    assert len(result.discrepancies) == 1
    d = result.discrepancies[0]
    assert d.kind == "wrong_target"
    assert d.backend == "ghdl"
    assert d.heuristic == "leaf_default"
    assert d.elaborated == "leaf_special"


def test_reconcile_confirms_matching_binding() -> None:
    g = _vhdl_graph()
    result = EnrichmentResult()
    _reconcile(g, {(("cfg_top", "rtl"), "u_leaf"): ("leaf_default", ["cfg_top.u_leaf"])}, result)

    assert result.discrepancies == []
    assert len(result.upgrades) == 1
    up = result.upgrades[0]
    assert up.src == "inst" and up.dst == "leaf_default"
    assert up.kind is EdgeKind.INSTANTIATES
    assert up.confidence == 1.0
    assert up.attrs["source"] == "elaborated" and up.attrs["backend"] == "ghdl"


def test_reconcile_unrolls_generate() -> None:
    # Multiplicity 4 → one instance_count discrepancy, annotation, and four
    # elaborated INSTANCE nodes added to the graph.
    g = _vhdl_graph()
    result = EnrichmentResult()
    paths = [f"gen_top.g_leaf({i}).u_leaf" for i in range(4)]
    _reconcile(g, {(("cfg_top", "rtl"), "u_leaf"): ("leaf_default", paths)}, result)

    assert any(d.kind == "instance_count" and d.elaborated == "4" for d in result.discrepancies)
    assert result.node_annotations["inst"] == {"elaborated_count": 4}
    new_ids = {n.id for n in result.new_nodes}
    assert elab_node_id(NodeKind.INSTANCE, "gen_top.g_leaf(0).u_leaf") in new_ids
    assert len([n for n in result.new_nodes if n.kind is NodeKind.INSTANCE]) == 4


# -- unconditional: static generate-range folding (no GHDL needed) -----------


class _AscendingRangeExpression:
    def __init__(self, left: int, right: int) -> None:
        self.LeftBound, self.RightBound = left, right


class _DescendingRangeExpression:
    def __init__(self, left: int, right: int) -> None:
        self.LeftBound, self.RightBound = left, right


class _ForGenerateStatement:
    """Stand-in whose class name carries 'For'/'Generate' for the walker."""

    def __init__(self, label: str, rng: object, body: list[object]) -> None:
        self.Label, self.Range, self.Statements = label, rng, body


class _EntityInstantiation:
    def __init__(self, label: str, entity: str) -> None:
        self.Label, self.Entity = label, entity


def _gen_stmt(rng: object) -> _ForGenerateStatement:
    return _ForGenerateStatement("g", rng, [])


def test_generate_count_respects_direction_and_null_ranges() -> None:
    assert _generate_count(_gen_stmt(_AscendingRangeExpression(0, 3))) == 4
    assert _generate_count(_gen_stmt(_DescendingRangeExpression(3, 0))) == 4
    # Null ranges elaborate zero bodies, not four.
    assert _generate_count(_gen_stmt(_AscendingRangeExpression(3, 0))) == 0
    assert _generate_count(_gen_stmt(_DescendingRangeExpression(0, 3))) == 0


# -- unconditional: ambiguous-label safe degradation -------------------------


def test_walk_statements_flags_label_reused_across_scopes() -> None:
    # `u_x` binds `foo` at top level and `bar` inside a generate: legal VHDL the
    # syntactic graph cannot tell apart, so the label is marked ambiguous.
    stmts = [
        _EntityInstantiation("u_x", "foo"),
        _ForGenerateStatement(
            "g", _AscendingRangeExpression(0, 1), [_EntityInstantiation("u_x", "bar")]
        ),
    ]
    collected: dict[str, tuple[str, list[str]]] = {}
    ambiguous: set[str] = set()
    _walk_statements(stmts, ["top"], collected, ambiguous)
    assert "u_x" in ambiguous


def test_walk_statements_same_entity_duplicate_is_not_ambiguous() -> None:
    # The legitimate generate-unroll case: one label, one entity, many paths.
    stmts = [
        _ForGenerateStatement(
            "g", _AscendingRangeExpression(0, 3), [_EntityInstantiation("u_leaf", "leaf")]
        ),
    ]
    collected: dict[str, tuple[str, list[str]]] = {}
    ambiguous: set[str] = set()
    _walk_statements(stmts, ["gen_top"], collected, ambiguous)
    assert ambiguous == set()
    entity, paths = collected["u_leaf"]
    assert entity == "leaf" and len(paths) == 4


def test_reconcile_skips_systemverilog_instances() -> None:
    # An SV instance (slang's territory) is ignored even if a binding matches.
    g = _vhdl_graph()
    g.nodes["inst"]["language"] = Language.SYSTEMVERILOG
    result = EnrichmentResult()
    _reconcile(g, {(("cfg_top", "rtl"), "u_leaf"): ("leaf_special", ["cfg_top.u_leaf"])}, result)
    assert result.discrepancies == [] and result.upgrades == []


# -- unconditional: a plain VHDL build is unchanged --------------------------


def test_default_vhdl_build_is_heuristic_only(tmp_path: Path, fixtures_dir: Path) -> None:
    import shutil as _sh

    _sh.copy(fixtures_dir / "cfg_override.vhd", tmp_path / "cfg_override.vhd")
    report = run_build(tmp_path)  # no enrich
    assert not report.enriched
    graph, _, _ = _db(tmp_path).load()
    assert not any(d["attrs"].get("source") == "elaborated" for _, d in graph.nodes(data=True))


# -- GHDL-gated acceptance ---------------------------------------------------


@ghdl
def test_enrich_unrolls_for_generate(tmp_path: Path, fixtures_dir: Path) -> None:
    import shutil as _sh

    _sh.copy(fixtures_dir / "vhdl_for_generate.vhd", tmp_path / "vhdl_for_generate.vhd")
    report = run_build(tmp_path, options=BuildOptions(enrich=True))
    assert report.enriched
    assert "ghdl" in report.enrich_backends

    graph, _, _ = _db(tmp_path).load()
    elaborated = [
        n
        for n, d in graph.nodes(data=True)
        if d["kind"] is NodeKind.INSTANCE and d["attrs"].get("source") == "elaborated"
    ]
    assert len(elaborated) == 4
    assert elab_node_id(NodeKind.INSTANCE, "gen_top.g_leaf(0).u_leaf") in graph
    items = _db(tmp_path).load_discrepancies()
    assert any(d.kind == "instance_count" and d.elaborated == "4" for d in items)


@ghdl
def test_enrich_confirms_default_binding(tmp_path: Path, fixtures_dir: Path) -> None:
    # vhdl_top's u_alu directly instantiates entity work.alu; GHDL confirms it,
    # upgrading the INSTANTIATES edge to 1.0 with ghdl provenance.
    import shutil as _sh

    for name in ("vhdl_top.vhd", "alu.vhd"):
        _sh.copy(fixtures_dir / name, tmp_path / name)
    run_build(tmp_path, options=BuildOptions(enrich=True))
    graph, _, _ = _db(tmp_path).load()
    inst = next(
        n
        for n, d in graph.nodes(data=True)
        if d["kind"] is NodeKind.INSTANCE and d["name"] == "u_alu"
    )
    upgraded = any(
        data["attrs"].get("backend") == "ghdl" and data["confidence"] == 1.0
        for _, _, data in graph.out_edges(inst, data=True)
        if data["kind"] is EdgeKind.INSTANTIATES
    )
    assert upgraded
