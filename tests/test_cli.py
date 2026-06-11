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
    assert "files parsed:   18" in result.output
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
    assert "18 parsed" in result.output
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
