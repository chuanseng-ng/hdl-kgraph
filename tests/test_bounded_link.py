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


def test_bounded_link_is_default(tmp_path: Path, fixtures_dir: Path) -> None:
    # A plain `update` (no flag) now takes the bounded path by default (v1.13.0).
    root = _project(tmp_path, fixtures_dir)
    (root / "extra.sv").write_text((root / "extra.sv").read_text() + "// touch\n")
    report = run_update(root)
    assert report.build is not None
    assert report.build.bounded_link is True


def test_selective_decode_skips_unaffected_clean_irs(
    tmp_path: Path, fixtures_dir: Path, monkeypatch
) -> None:
    # Editing a leaf module nothing depends on: the bounded (default) path decodes
    # NO clean IRs (dirty units are parsed fresh; clean units are macro-replayed
    # only), so ir_from_json is never called. The legacy path decodes every clean IR.
    import hdl_kgraph.storage.ir_codec as ir_codec
    from hdl_kgraph import pipeline

    root = _project(tmp_path, fixtures_dir)
    real = ir_codec.ir_from_json
    calls: list[int] = []

    def _counting(text: str):
        calls.append(1)
        return real(text)

    # editing extra.sv (a module nothing instantiates) affects no other unit
    (root / "extra.sv").write_text((root / "extra.sv").read_text() + "// touch\n")
    monkeypatch.setattr(pipeline.ir_codec, "ir_from_json", _counting)
    report = run_update(root)  # bounded default
    assert report.build is not None and report.build.bounded_link is True
    assert calls == [], "bounded selective decode must not decode any clean IR here"


def test_no_bounded_link_opts_out(tmp_path: Path, fixtures_dir: Path) -> None:
    # --no-bounded-link falls back to the in-memory re-link (still byte-identical).
    root = _project(tmp_path, fixtures_dir)
    (root / "extra.sv").write_text((root / "extra.sv").read_text() + "// touch\n")
    report = run_update(root, options=BuildOptions(bounded_link=False))
    assert report.build is not None
    assert report.build.bounded_link is False
    assert report.build.incremental_link is True
