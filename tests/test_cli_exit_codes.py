"""The CLI exit-code contract (#73): one policy, text and --json in agreement.

0  success — including an empty *report*
1  a documented negative result (detect-changes dirty; a name lookup that
   matched nothing), in JSON exactly as in text
2  any error (bad usage, missing/foreign DB, config/VCS failure, unexpected
   build/update failure)
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A minimal built design: top instantiates leaf; no clocks/CDC."""
    (tmp_path / "leaf.sv").write_text(
        "module leaf(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    (tmp_path / "top.sv").write_text(
        "module top(input logic a, output logic y);\n  leaf u(.a(a), .y(y));\nendmodule\n"
    )
    assert CliRunner().invoke(main, ["build", str(tmp_path)]).exit_code == 0
    return tmp_path


def _db(built: Path) -> list[str]:
    return ["--db", str(built / ".hdl-kgraph" / "graph.db")]


# --- 0: success, including empty reports -----------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["query", "modules"],
        ["query", "clock-domains"],  # design has no clocks -> empty report, still 0
        ["query", "cdc"],  # no CDC suspects -> good news, still 0
        ["query", "reset-tree"],
        ["query", "uvm"],
        ["query", "unresolved"],
        ["status"],
        ["tree"],
    ],
)
def test_report_commands_exit_zero_even_when_empty(built: Path, args: list[str]) -> None:
    for extra in ([], ["--json"]):
        if "--json" in extra and args[-1] in {"tree", "status"}:
            continue  # these have no --json mode
        result = CliRunner().invoke(main, [*args, *_db(built), *extra])
        assert result.exit_code == 0, (args, extra, result.output)


# --- 1: name-lookup negative result, text and JSON agree -------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["query", "instances-of", "no_such_unit"],
        ["query", "drivers", "no_such_signal"],
    ],
)
def test_name_lookup_miss_exits_one_in_text_and_json(built: Path, cmd: list[str]) -> None:
    text = CliRunner().invoke(main, [*cmd, *_db(built)])
    assert text.exit_code == 1, text.output
    js = CliRunner().invoke(main, [*cmd, "--json", *_db(built)])
    assert js.exit_code == 1, js.output  # the divergence #73 fixes


def test_name_lookup_hit_exits_zero(built: Path) -> None:
    hit = CliRunner().invoke(main, ["query", "instances-of", "leaf", *_db(built)])
    assert hit.exit_code == 0, hit.output


# --- 2: errors --------------------------------------------------------------


def test_missing_database_exits_two(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["status", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 2, result.output


def test_foreign_database_exits_two(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    db.write_bytes(b"not a sqlite database\n" * 4)
    result = CliRunner().invoke(main, ["query", "modules", "--db", str(db)])
    assert result.exit_code == 2, result.output


def test_unexpected_build_failure_is_clean_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ClickException from the pipeline surfaces as a clean exit 2, not a
    raw traceback."""
    import hdl_kgraph.cli.main as cli

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_build", boom)
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "build failed" in result.output
    assert "kaboom" in result.output
