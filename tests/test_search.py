"""search_nodes / signal_drivers / impact_seeds analysis tests (M6)."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import analysis
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.storage.sqlite_store import FileMeta


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    vhdl = VhdlParser()
    irs = [
        sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        sv.parse(Path("top_positional.v"), (fixtures_dir / "top_positional.v").read_text()),
        sv.parse(Path("adder.v"), (fixtures_dir / "adder.v").read_text()),
        vhdl.parse(Path("alu.vhd"), (fixtures_dir / "alu.vhd").read_text()),
    ]
    return build_graph(irs)


def test_search_glob_and_kind_filter(graph) -> None:
    records = analysis.search_nodes(graph, name="df_*", kinds=[NodeKind.MODULE])
    assert [r["name"] for r in records] == ["df_sub", "df_top"]
    assert all(not r["unresolved"] for r in records)


def test_search_file_filter(graph) -> None:
    records = analysis.search_nodes(graph, kinds=[NodeKind.MODULE], file="*positional*")
    assert [r["name"] for r in records] == ["top_positional"]


def test_search_vhdl_case_insensitive(graph) -> None:
    records = analysis.search_nodes(graph, name="ALU", kinds=[NodeKind.ENTITY])
    assert len(records) == 1
    assert records[0]["language"] is Language.VHDL


def test_search_qualified_name_pattern(graph) -> None:
    records = analysis.search_nodes(graph, name="df_top.stage")
    assert [r["qualified_name"] for r in records] == ["df_top.stage"]


def test_signal_drivers_matches_by_name(graph) -> None:
    records = analysis.signal_drivers(graph, "stage")
    assert records
    assert all(r["module"] == "df_top" for r in records)
    assert all(r["site_kind"] == "process" for r in records)


def test_signal_drivers_module_scope(graph) -> None:
    in_sub = analysis.signal_drivers(graph, "o", module="df_sub")
    assert in_sub  # df_sub's always_ff drives its o port
    assert analysis.signal_drivers(graph, "o", module="df_top") == []


def test_signal_drivers_vhdl_architecture_maps_to_entity(graph) -> None:
    records = analysis.signal_drivers(graph, "result", module="alu")
    assert records  # the alu process drives result; scope given as the entity
    assert analysis.signal_drivers(graph, "result", module="df_top") == []


def test_signal_drivers_readers(graph) -> None:
    readers = analysis.signal_drivers(graph, "stage", readers=True)
    assert readers
    assert all(r["signal"] == "df_top.stage" for r in readers)


def _files(*paths: str) -> list[FileMeta]:
    return [
        FileMeta(path=p, language=Language.SYSTEMVERILOG, content_hash="", size_bytes=0)
        for p in paths
    ]


def test_impact_seeds_by_file_path(graph) -> None:
    seeds = analysis.impact_seeds(graph, _files("dataflow.sv", "adder.v"), "dataflow.sv")
    assert seeds == ["file:dataflow.sv"]


def test_impact_seeds_by_unit_name(graph) -> None:
    seeds = analysis.impact_seeds(graph, _files("adder.v"), "adder")
    assert len(seeds) == 1
    assert graph.nodes[seeds[0]]["name"] == "adder"


def test_impact_seeds_unknown_target(graph) -> None:
    assert analysis.impact_seeds(graph, _files("adder.v"), "nothing_here") == []
