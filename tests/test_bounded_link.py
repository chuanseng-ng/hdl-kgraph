"""Focused tests for the opt-in memory-bounded incremental linker (#119).

The byte-identical *graph* equivalence (both link paths, incl. fuzz) lives in
``tests/test_incremental_equivalence.py``; here we cover the CLI flag wiring, the
``bounded_link`` report flag, and that the whole-design summaries + counts are
refreshed correctly from the DB on the bounded path (it never holds the whole
graph in memory).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.config import BuildOptions
from hdl_kgraph.pipeline import default_db_path, run_build, run_update
from hdl_kgraph.storage.sqlite_store import SqliteStore


def _project(tmp: Path, fixtures_dir: Path) -> Path:
    (tmp / "two_clock_cdc.sv").write_text((fixtures_dir / "two_clock_cdc.sv").read_text())
    (tmp / "extra.sv").write_text(
        "module extra(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    run_build(tmp)
    return tmp


def test_bounded_update_sets_flag_and_matches_full(tmp_path: Path, fixtures_dir: Path) -> None:
    root = _project(tmp_path, fixtures_dir)
    (root / "extra.sv").write_text((root / "extra.sv").read_text() + "// touch\n")

    report = run_update(root, options=BuildOptions(bounded_link=True))
    assert report.build is not None
    assert report.build.bounded_link is True
    assert report.build.incremental_link is True

    db = default_db_path(root)
    bounded_clock = SqliteStore(db).load_summary("clock_domains")
    graph_b, _f, _m = SqliteStore(db).load()
    counts_b = (graph_b.number_of_nodes(), graph_b.number_of_edges())
    # report counts (read from DB on the bounded path) match the loaded graph
    assert (report.build.node_count, report.build.edge_count) == counts_b

    # a fresh full build is the ground truth for graph + summaries
    run_build(root)
    full_clock = SqliteStore(db).load_summary("clock_domains")
    graph_f, _f2, _m2 = SqliteStore(db).load()
    assert counts_b == (graph_f.number_of_nodes(), graph_f.number_of_edges())
    # the bounded path refreshed the clock/CDC summary out-of-core, byte-identical
    assert bounded_clock is not None and full_clock is not None
    assert json.loads(bounded_clock) == json.loads(full_clock)
    # and it found the fixture's two domains
    assert len(json.loads(bounded_clock)["domains"]) == 2


def test_cli_update_bounded_link(tmp_path: Path, fixtures_dir: Path) -> None:
    root = _project(tmp_path, fixtures_dir)
    (root / "extra.sv").write_text((root / "extra.sv").read_text() + "// touch\n")
    result = CliRunner().invoke(main, ["update", str(root), "--bounded-link"])
    assert result.exit_code == 0, result.output
    assert "updated in" in result.output
