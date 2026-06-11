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


def test_build_reports_summary(project: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(project)])
    assert result.exit_code == 0
    assert "files parsed:   25" in result.output
    assert "vhdl files:     5" in result.output
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


def test_status(project: Path) -> None:
    result = CliRunner().invoke(main, ["status", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "25 parsed" in result.output
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
