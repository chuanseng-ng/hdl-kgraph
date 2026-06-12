"""End-to-end CLI tests: build -> status -> query -> tree on the fixtures."""

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph import __version__
from hdl_kgraph.cli.main import main


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    """A tmp copy of the fixture corpus with a graph already built."""
    root = tmp_path_factory.mktemp("project")
    for path in fixtures_dir.iterdir():
        if path.is_file():  # subdirectories (e.g. preproc/) have their own tests
            shutil.copy(path, root / path.name)
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    return root


def db_args(project: Path) -> list[str]:
    return ["--db", str(project / ".hdl-kgraph" / "graph.db")]


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("build", "status", "query", "tree"):
        assert command in result.output


def _hdl_fixture_count(fixtures_dir: Path, *suffixes: str) -> int:
    return sum(1 for p in fixtures_dir.iterdir() if p.is_file() and p.suffix in suffixes)


def test_build_reports_summary(project: Path, fixtures_dir: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(project)])
    assert result.exit_code == 0
    parsed = _hdl_fixture_count(fixtures_dir, ".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl")
    vhdl = _hdl_fixture_count(fixtures_dir, ".vhd", ".vhdl")
    assert f"files parsed:   {parsed}" in result.output
    assert f"vhdl files:     {vhdl}" in result.output
    assert "parse errors:" in result.output  # broken.sv
    assert "unresolved:" in result.output  # ghost_mod etc.


def test_build_empty_dir_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code != 0
    assert "no parseable HDL files" in result.output


def test_failed_build_preserves_existing_db(project: Path, tmp_path: Path) -> None:
    db = project / ".hdl-kgraph" / "graph.db"
    before = db.read_bytes()
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--db", str(db)])
    assert result.exit_code != 0
    assert db.read_bytes() == before


def test_status(project: Path, fixtures_dir: Path) -> None:
    result = CliRunner().invoke(main, ["status", *db_args(project)])
    assert result.exit_code == 0, result.output
    parsed = _hdl_fixture_count(fixtures_dir, ".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl")
    assert f"{parsed} parsed" in result.output
    assert "parse error(s)" in result.output
    assert "module" in result.output
    assert "instantiates" in result.output
    assert "unresolved:" in result.output


def test_status_without_db_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code != 0
    assert "hdl-kgraph build" in result.output


def test_query_instances_of(project: Path) -> None:
    result = CliRunner().invoke(
        main, ["query", "instances-of", "simple_counter", *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    assert "top.u_counter" in result.output
    assert "top.v:" in result.output
    assert "confidence=0.8" in result.output


def test_query_instances_of_unknown_name(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "instances-of", "nonexistent", *db_args(project)])
    assert result.exit_code == 1


def test_query_modules(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "modules", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "simple_counter" in result.output
    assert "instances=1" in result.output


def test_query_unresolved(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "unresolved", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "module:ghost_mod" in result.output
    assert "class:uvm_test" in result.output


def test_query_clock_domains(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "clock-domains", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "two_clock_top.clk_a" in result.output
    assert "processes:" in result.output


def test_query_cdc_finds_the_planted_crossing(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "cdc", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "data_a" in result.output
    assert "clk_a ->" in result.output


def test_query_cdc_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["query", "cdc", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert any(item["signal_name"] == "data_a" for item in payload)


def test_query_reset_tree(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "reset-tree", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "rst_n" in result.output
    assert "async" in result.output


def test_query_drivers(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "drivers", "data_a", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "two_clock_top.always@22" in result.output


def test_query_drivers_readers(project: Path) -> None:
    result = CliRunner().invoke(
        main, ["query", "drivers", "data_a", "--readers", *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    assert "two_clock_top.always@28" in result.output


def test_lint_reports_planted_findings(project: Path) -> None:
    result = CliRunner().invoke(main, ["lint", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "unconnected-port" in result.output
    assert "undriven-signal" in result.output
    assert "redundant-override" in result.output


def test_lint_check_filter_and_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["lint", "--check", "open-port", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert payload and all(item["check"] == "open-port" for item in payload)


def test_lint_unknown_check_fails(project: Path) -> None:
    result = CliRunner().invoke(main, ["lint", "--check", "bogus", *db_args(project)])
    assert result.exit_code != 0
    assert "unknown lint check" in result.output


def test_query_uvm(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "uvm", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "test:" in result.output
    assert "verif_smoke_test" in result.output
    assert "covers verif_dut" in result.output


def test_visualize_writes_html(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "g.html"
    result = CliRunner().invoke(main, ["visualize", "-o", str(out), *db_args(project)])
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "simple_counter" in html


def test_metrics_lists_hubs(project: Path) -> None:
    result = CliRunner().invoke(main, ["metrics", "--top", "0", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "fan-in" in result.output
    assert "alu" in result.output


def test_metrics_communities_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["metrics", "--communities", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert payload["modules"]
    assert payload["communities"]


def test_tree_from_top(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "top"
    assert "u_counter: simple_counter" in result.output


def test_tree_marks_unresolved_and_ambiguous(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "u_ghost: ghost_mod [?]" in result.output
    assert "u_leaf: dup_leaf [~0.6]" in result.output


def test_tree_unknown_module(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "nonexistent", *db_args(project)])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_db_discovery_walks_up(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subdir = project / "sub" / "dir"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output


def test_tree_mixed_sv_top_shows_vhdl_leaf(project: Path) -> None:
    """Verilog-top / VHDL-leaf acceptance: one connected hierarchy."""
    result = CliRunner().invoke(main, ["tree", "mixed_sv_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "mixed_sv_top"
    assert "u_alu: alu(rtl)" in result.output


def test_tree_vhdl_top_shows_sv_leaves(project: Path) -> None:
    """VHDL-top / Verilog-leaf acceptance, both instantiation styles."""
    result = CliRunner().invoke(main, ["tree", "vhdl_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "vhdl_top(rtl)"
    assert "u_counter: simple_counter" in result.output
    assert "u_fifo: FIFO" in result.output  # case-folded component match
    assert "u_alu: alu(rtl)" in result.output


def test_tree_honors_configuration_override(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "cfg_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "u_leaf: leaf_special(rtl)" in result.output
    assert "leaf_default" not in result.output


def test_tree_vhdl_name_is_case_insensitive(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "VHDL_Top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "vhdl_top(rtl)"


def test_query_instances_of_spans_languages(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "instances-of", "alu", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "mixed_sv_top.sv:" in result.output
    assert "vhdl_top.vhd:" in result.output
    assert "confidence=0.8" in result.output


def test_query_modules_lists_entities(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "modules", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "alu [vhdl]" in result.output


def test_build_lib_flag(tmp_path: Path, fixtures_dir: Path) -> None:
    from hdl_kgraph.storage.sqlite_store import SqliteStore

    libdir = tmp_path / "mylib_src"
    libdir.mkdir()
    shutil.copy(fixtures_dir / "alu.vhd", libdir / "alu.vhd")
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--lib", f"mylib={libdir}"])
    assert result.exit_code == 0, result.output
    graph, _, _ = SqliteStore(tmp_path / ".hdl-kgraph" / "graph.db").load()
    lib = graph.nodes["library:mylib"]
    assert lib["attrs"]["path"] == str(libdir)
    entity = graph.nodes["mylib_src/alu.vhd::entity:alu"]
    assert entity["attrs"]["library"] == "mylib"


def test_build_lib_flag_malformed(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--lib", "nopath"])
    assert result.exit_code != 0
    assert "NAME=PATH" in result.output


# -- M4: update / detect-changes / impact ------------------------------------


@pytest.fixture
def small_project(tmp_path: Path) -> Path:
    (tmp_path / "leaf.sv").write_text(
        "module leaf(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    (tmp_path / "top.sv").write_text(
        "module top(input logic a, output logic y);\n  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
    )
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_help_lists_m4_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("update", "detect-changes", "impact", "watch"):
        assert command in result.output


def test_update_up_to_date(small_project: Path) -> None:
    result = CliRunner().invoke(main, ["update", str(small_project)])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_update_reports_reparsed_files(small_project: Path) -> None:
    leaf = small_project / "leaf.sv"
    leaf.write_text(leaf.read_text() + "// touched\n")
    result = CliRunner().invoke(main, ["update", str(small_project)])
    assert result.exit_code == 0, result.output
    assert "re-parsed: leaf.sv (changed)" in result.output
    assert "files reused:   1" in result.output


def test_update_without_db_does_full_build(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    result = CliRunner().invoke(main, ["update", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "full rebuild: no existing database" in result.output


def test_update_full_flag(small_project: Path) -> None:
    result = CliRunner().invoke(main, ["update", str(small_project), "--full"])
    assert result.exit_code == 0, result.output
    assert "full rebuild: forced with --full" in result.output


def test_detect_changes_exit_codes(small_project: Path) -> None:
    clean = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert clean.exit_code == 0, clean.output
    assert clean.output == ""

    (small_project / "leaf.sv").write_text("module leaf;\nendmodule\n")
    (small_project / "new.sv").write_text("module new_mod;\nendmodule\n")
    dirty = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert dirty.exit_code == 1
    assert "M leaf.sv" in dirty.output
    assert "A new.sv" in dirty.output


def test_detect_changes_reports_deletions(small_project: Path) -> None:
    (small_project / "leaf.sv").unlink()
    result = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert result.exit_code == 1
    assert "D leaf.sv" in result.output


def test_detect_changes_without_db_fails(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    result = CliRunner().invoke(main, ["detect-changes", str(tmp_path)])
    assert result.exit_code != 0
    assert "hdl-kgraph build" in result.output


def test_detect_changes_closure(tmp_path: Path) -> None:
    (tmp_path / "defs.svh").write_text("`define W 8\n")
    (tmp_path / "leaf.sv").write_text(
        '`include "defs.svh"\nmodule leaf(output logic [`W-1:0] y);\nendmodule\n'
    )
    assert CliRunner().invoke(main, ["build", str(tmp_path)]).exit_code == 0
    (tmp_path / "defs.svh").write_text("`define W 16\n")
    db = str(tmp_path / ".hdl-kgraph" / "graph.db")
    result = CliRunner().invoke(main, ["detect-changes", str(tmp_path), "--closure", "--db", db])
    assert result.exit_code == 1
    assert "M defs.svh" in result.output
    assert "~ leaf.sv (includes defs.svh)" in result.output


def test_impact_module(small_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(small_project)
    result = CliRunner().invoke(main, ["impact", "leaf"])
    assert result.exit_code == 0, result.output
    assert "top" in result.output
    assert "instantiates (depth 1)" in result.output


def test_impact_file_target_and_files_flag(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "leaf.sv", *db])
    assert result.exit_code == 0, result.output
    assert "leaf" in result.output and "top" in result.output

    files = CliRunner().invoke(main, ["impact", "leaf.sv", "--files", *db])
    assert files.exit_code == 0, files.output
    assert "top.sv" in files.output


def test_impact_unknown_target(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "ghost", *db])
    assert result.exit_code != 0
    assert "matches no file or design unit" in result.output


def test_impact_top_has_no_dependents(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "top", *db])
    assert result.exit_code == 0, result.output
    assert "no dependents found" in result.output
