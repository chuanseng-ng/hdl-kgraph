"""The ``tools`` CLI group: MCP tools exposed as JSON-printing subcommands.

These commands are the MCP-free read surface. Each one is a thin wrapper over a
:class:`~hdl_kgraph.storage.query.GraphQuery` method, so the contract here is
narrow: the JSON a command prints must equal what calling the method directly
returns (``test_query.py`` already pins those methods against the full-graph
path). We also check the not-found / bad-filter error paths exit cleanly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.cli.render import json_default
from hdl_kgraph.storage.query import GraphQuery


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("tools_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture(scope="module")
def db_path(project: Path) -> Path:
    return project / ".hdl-kgraph" / "graph.db"


def _run(db_path: Path, *args: str) -> Any:
    result = CliRunner().invoke(main, ["tools", *args, "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _roundtrip(value: Any) -> Any:
    """The JSON the CLI would print for a ``GraphQuery`` result (so a direct
    method call and the command compare on equal footing)."""
    return json.loads(json.dumps(value, default=json_default))


def test_find_module_matches_query(db_path: Path) -> None:
    cli = _run(db_path, "find-module", "*", "--limit", "5")
    direct = _roundtrip(GraphQuery(db_path).find_module("*", 5))
    assert cli == direct


def test_hierarchy_tops_then_tree(db_path: Path) -> None:
    tops = _run(db_path, "hierarchy")
    assert "tops" in tops and tops["tops"]
    name = tops["tops"][0]["name"]
    cli = _run(db_path, "hierarchy", name, "--depth", "2")
    direct = _roundtrip(GraphQuery(db_path).hierarchy(name, 2, 500))
    assert cli == direct


def test_search_nodes_matches_query(db_path: Path) -> None:
    from hdl_kgraph.schema import NodeKind

    cli = _run(db_path, "search-nodes", "*", "--kind", "module")
    direct = _roundtrip(GraphQuery(db_path).search_nodes("*", [NodeKind.MODULE], None, 50, 0))
    assert cli == direct


def test_clock_domains_and_uvm_are_json(db_path: Path) -> None:
    assert _run(db_path, "clock-domains") == _roundtrip(GraphQuery(db_path).clock_domains())
    assert _run(db_path, "uvm-topology") == _roundtrip(GraphQuery(db_path).uvm_topology())


def test_unknown_top_exits_two(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["tools", "hierarchy", "no_such_unit", "--db", str(db_path)])
    assert result.exit_code == 2
    assert "no_such_unit" in result.output


def test_bad_kind_exits_two(db_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["tools", "search-nodes", "--kind", "bogus", "--db", str(db_path)]
    )
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_works_without_fastmcp(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """The group must not require the ``[mcp]`` extra — block ``fastmcp`` and
    confirm a command still succeeds."""
    import builtins

    real_import = builtins.__import__

    def _no_fastmcp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fastmcp" or name.startswith("fastmcp."):
            raise ImportError("fastmcp blocked for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastmcp)
    result = CliRunner().invoke(main, ["tools", "find-module", "*", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "items" in json.loads(result.output)
