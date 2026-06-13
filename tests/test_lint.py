"""Lint-flavored analysis tests (M5) and waiver matching."""

from pathlib import Path

import pytest

from hdl_kgraph.config import LintWaiver
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


# -- waivers -------------------------------------------------------------------


def _waiver(**kwargs) -> LintWaiver:
    kwargs.setdefault("reason", "test")
    return LintWaiver(**kwargs)


def test_findings_carry_owning_unit(findings) -> None:
    units = {f.check: f.unit for f in findings}
    assert units["unconnected-port"] == "lint_leaf"
    assert units["open-port"] == "lint_leaf"
    assert units["undriven-signal"] == "lint_top"
    assert units["unread-signal"] == "lint_top"
    assert units["dead-module"] == "lint_dead"
    assert units["redundant-override"] == "lint_leaf"


def test_waiver_by_exact_name(findings) -> None:
    waivers = [_waiver(check="undriven-signal", name="lint_top.undriven_bus", reason="known")]
    result = lint.apply_waivers(findings, waivers)
    assert [w.finding.check for w in result.waived] == ["undriven-signal"]
    assert result.waived[0].reason == "known"
    assert result.waived[0].waiver_index == 0
    assert result.unused == [] and result.unknown == []
    assert "undriven-signal" not in {f.check for f in result.kept}


def test_waiver_partition_is_total_and_ordered(findings) -> None:
    result = lint.apply_waivers(findings, [_waiver(check="open-port", name="lint_top.*")])
    assert len(result.kept) + len(result.waived) == len(findings)
    assert result.kept == [f for f in findings if f.check != "open-port"]


def test_waiver_plain_name_matches_last_segment(findings) -> None:
    result = lint.apply_waivers(findings, [_waiver(check="unread-signal", name="unread_*")])
    assert [w.finding.name for w in result.waived] == ["lint_top.unread_wire"]


def test_waiver_name_is_case_sensitive(findings) -> None:
    result = lint.apply_waivers(findings, [_waiver(check="unread-signal", name="UNREAD_*")])
    assert result.waived == []
    assert result.unused == [0]


def test_waiver_by_file_glob(findings) -> None:
    result = lint.apply_waivers(findings, [_waiver(check="dead-module", file="lint_*.sv")])
    assert [w.finding.name for w in result.waived] == ["lint_dead"]


def test_waiver_criteria_and_together(findings) -> None:
    wrong_line = _waiver(check="undriven-signal", name="lint_top.undriven_bus", line=1)
    result = lint.apply_waivers(findings, [wrong_line])
    assert result.waived == []
    assert result.unused == [0]
    line = next(f for f in findings if f.check == "undriven-signal").line
    assert line > 1
    result = lint.apply_waivers(
        findings, [_waiver(check="undriven-signal", name="lint_top.undriven_bus", line=line)]
    )
    assert len(result.waived) == 1


def test_waiver_unused_only_for_selected_checks(findings) -> None:
    stale = _waiver(check="unread-signal", name="no_such_signal")
    open_only = [f for f in findings if f.check == "open-port"]
    result = lint.apply_waivers(open_only, [stale], selected=["open-port"])
    assert result.unused == []  # unread-signal did not run: not stale
    result = lint.apply_waivers(findings, [stale])
    assert result.unused == [0]


def test_waiver_unknown_check_reported(findings) -> None:
    result = lint.apply_waivers(findings, [_waiver(check="no-such-check", name="*")])
    assert result.unknown == [0]
    assert result.unused == []  # unknown is reported separately, not as stale


def test_module_waiver_covers_repeated_instances() -> None:
    src = """
module wleaf(input logic a, output logic y);
  assign y = a;
endmodule
module wtop(input logic a);
  wleaf u0 (.a(a));
  wleaf u1 (.a(a));
endmodule
"""
    sv = SystemVerilogParser()
    g = build_graph([sv.parse(Path("w.sv"), src)])
    findings = lint.run_checks(g, names=["unconnected-port"])
    assert len(findings) == 2
    assert all(f.unit == "wleaf" for f in findings)
    result = lint.apply_waivers(
        findings, [_waiver(check="unconnected-port", module="wleaf")], ["unconnected-port"]
    )
    assert result.kept == []
    assert len(result.waived) == 2
    assert result.unused == []
