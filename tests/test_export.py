"""Graph export tests (viz-scalability Phase 5): GraphML/GEXF/JSON.

The exporter sanitizes the graph (enums, the ``line_span`` tuple, and the
free-form ``attrs`` dict) into scalar attributes, then leans on NetworkX's
writers. These tests pin the round-trips and the attribute flattening.
"""

import json
import shutil
from pathlib import Path

import networkx as nx
import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.export import EXPORT_FORMATS, export_graph
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    irs = [
        sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        sv.parse(Path("two_clock_cdc.sv"), (fixtures_dir / "two_clock_cdc.sv").read_text()),
    ]
    return build_graph(irs)


def test_graphml_round_trips(graph, tmp_path: Path) -> None:
    out = export_graph(graph, tmp_path / "g.graphml", "graphml")
    assert out.is_file()
    reloaded = nx.read_graphml(out)
    assert reloaded.number_of_nodes() == graph.number_of_nodes()
    assert reloaded.number_of_edges() == graph.number_of_edges()
    # Enums survive as their plain string values, not "NodeKind.MODULE".
    kinds = {d.get("kind") for _, d in reloaded.nodes(data=True)}
    assert "module" in kinds
    assert not any(k and k.startswith("NodeKind") for k in kinds)


def test_gexf_round_trips(graph, tmp_path: Path) -> None:
    out = export_graph(graph, tmp_path / "g.gexf", "gexf")
    reloaded = nx.read_gexf(out)
    assert reloaded.number_of_nodes() == graph.number_of_nodes()
    assert reloaded.number_of_edges() == graph.number_of_edges()


def test_json_is_node_link_with_string_enums(graph, tmp_path: Path) -> None:
    out = export_graph(graph, tmp_path / "g.json", "json")
    data = json.loads(out.read_text())
    assert {"nodes", "links"} <= set(data)
    assert len(data["nodes"]) == graph.number_of_nodes()
    assert all(isinstance(n["kind"], str) for n in data["nodes"])


def test_line_span_flattened_and_attrs_preserved(graph, tmp_path: Path) -> None:
    out = export_graph(graph, tmp_path / "g.graphml", "graphml")
    reloaded = nx.read_graphml(out)
    # The tuple span is split into two scalar columns the writers can take.
    assert any("line_start" in d and "line_end" in d for _, d in reloaded.nodes(data=True))
    assert not any("line_span" in d for _, d in reloaded.nodes(data=True))
    # attrs is preserved losslessly as a parseable JSON string.
    for _, d in reloaded.nodes(data=True):
        assert isinstance(json.loads(d["attrs_json"]), dict)


def test_unknown_format_raises(graph, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="format must be one of"):
        export_graph(graph, tmp_path / "g.dot", "dot")


def test_export_formats_surface() -> None:
    assert EXPORT_FORMATS == ("graphml", "gexf", "json")


def test_cli_export_smoke(tmp_path_factory, fixtures_dir: Path) -> None:
    root = tmp_path_factory.mktemp("export_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    runner = CliRunner()
    built = runner.invoke(main, ["build", str(root)])
    assert built.exit_code == 0, built.output
    db = ["--db", str(root / ".hdl-kgraph" / "graph.db")]
    out = root / "g.graphml"
    result = runner.invoke(main, ["export", "--format", "graphml", "-o", str(out), *db])
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert out.is_file()
    assert nx.read_graphml(out).number_of_nodes() > 0
