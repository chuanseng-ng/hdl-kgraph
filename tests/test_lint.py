"""Lint-flavored analysis tests (M5)."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import lint
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    return build_graph(
        [sv.parse(Path("lint_case.sv"), (fixtures_dir / "lint_case.sv").read_text())]
    )


@pytest.fixture(scope="module")
def findings(graph):
    return lint.run_checks(graph, tops=frozenset({"lint_top"}))


def _by_check(findings, check):
    return [f for f in findings if f.check == check]


def test_unconnected_ports(findings) -> None:
    found = _by_check(findings, "unconnected-port")
    assert {f.message.split("'")[1] for f in found} == {"q", "extra"}
    assert all(f.name == "lint_top.u_leaf" for f in found)


def test_open_port(findings) -> None:
    found = _by_check(findings, "open-port")
    assert len(found) == 1
    assert "'en'" in found[0].message


def test_undriven_signal(findings) -> None:
    found = _by_check(findings, "undriven-signal")
    assert [f.name for f in found] == ["lint_top.undriven_bus"]


def test_unread_signal(findings) -> None:
    found = _by_check(findings, "unread-signal")
    assert [f.name for f in found] == ["lint_top.unread_wire"]


def test_dead_module_respects_tops(findings) -> None:
    found = _by_check(findings, "dead-module")
    assert [f.name for f in found] == ["lint_dead"]
    assert all(f.confidence == 0.4 for f in found)


def test_redundant_override(findings) -> None:
    found = _by_check(findings, "redundant-override")
    assert len(found) == 1
    assert "'WIDTH'" in found[0].message


def test_clean_fixture_stays_clean(fixtures_dir: Path) -> None:
    sv = SystemVerilogParser()
    g = build_graph(
        [
            sv.parse(Path("dataflow.sv"), (fixtures_dir / "dataflow.sv").read_text()),
        ]
    )
    noisy = lint.run_checks(
        g,
        names=["unconnected-port", "open-port", "redundant-override"],
        tops=frozenset({"df_top"}),
    )
    assert noisy == []


def test_unknown_check_rejected(graph) -> None:
    with pytest.raises(ValueError, match="unknown lint check"):
        lint.run_checks(graph, names=["no-such-check"])


def test_error_files_suppress_signal_checks(graph) -> None:
    suppressed = lint.run_checks(
        graph,
        names=["undriven-signal", "unread-signal"],
        error_files=frozenset({"lint_case.sv"}),
    )
    assert suppressed == []
